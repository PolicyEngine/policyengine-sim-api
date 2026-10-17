"""Backend protocol and Stage 5 old-gateway implementation."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Protocol

import httpx
from policyengine_observability import (
    REQUEST_ID_HEADER,
    ObservabilityRuntime,
    instrument_httpx,
)
from policyengine_simulation_observability.identifiers import OBSERVABILITY_ID_HEADER
from pydantic import ValidationError

from policyengine_simulation_entry.schemas import BackendServiceToken

if TYPE_CHECKING:
    from policyengine_simulation_contract.json_types import JsonObject

    from policyengine_simulation_entry.config import Settings

SAFE_RESPONSE_HEADERS = frozenset(
    {
        "cache-control",
        "content-type",
        "etag",
        "retry-after",
        REQUEST_ID_HEADER.lower(),
        OBSERVABILITY_ID_HEADER.lower(),
    }
)


class BackendUnavailableError(RuntimeError):
    """The backend could not be reached."""


class BackendTimeoutError(RuntimeError):
    """The backend did not answer within the configured timeout."""


class BackendAuthenticationError(BackendUnavailableError):
    """The service could not authenticate to the backend."""


@dataclass(frozen=True)
class BackendResponse:
    """A backend reply with only the safe-to-forward response headers kept."""

    status_code: int
    content: bytes
    headers: dict[str, str]


class SimulationBackend(Protocol):
    """Backend the entry routes forward simulation requests to.

    The app starts it in the lifespan and closes it on shutdown.
    """

    async def start(self) -> None:
        """Open connections; called once when the app starts."""
        ...

    async def close(self) -> None:
        """Release connections; called when the app shuts down."""
        ...

    async def ready(self) -> bool:
        """Return whether the backend can serve requests, for the /ready probe."""
        ...

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: JsonObject | None = None,
        request_id: str | None = None,
        observability_id: str | None = None,
    ) -> BackendResponse:
        """Send one request and return the backend's response.

        Raise BackendUnavailableError or BackendTimeoutError when no response arrives.
        """
        ...


class ClientCredentialsTokenProvider:
    """Fetch and cache the Simulation Entrypoint's old-gateway M2M token."""

    REFRESH_MARGIN_SECONDS = 60

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        """Start with no cached token; the first get_token call fetches one."""
        self.settings = settings
        self.client = client
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        """Return a bearer token, refetching it within a minute of expiry.

        Raise BackendAuthenticationError if Auth0 cannot issue one.
        """
        async with self._lock:
            now = time.time()
            if (
                self._token is None
                or now >= self._expires_at - self.REFRESH_MARGIN_SECONDS
            ):
                await self._fetch()
            if self._token is None:  # pragma: no cover
                msg = "Auth0 returned no token."
                raise BackendAuthenticationError(msg)
            return self._token

    def invalidate(self) -> None:
        """Drop the cached token so the next get_token call fetches a new one."""
        self._token = None
        self._expires_at = 0.0

    async def _fetch(self) -> None:
        try:
            response = await self.client.post(
                f"{self.settings.old_gateway_auth_issuer}oauth/token",
                json={
                    "client_id": self.settings.old_gateway_auth_client_id,
                    "client_secret": self.settings.old_gateway_auth_client_secret,
                    "audience": self.settings.old_gateway_auth_audience,
                    "grant_type": "client_credentials",
                },
            )
            response.raise_for_status()
            token = BackendServiceToken.model_validate(response.json())
            self._token = token.access_token
            self._expires_at = time.time() + token.expires_in
        except BackendAuthenticationError:
            raise
        except (httpx.HTTPError, TypeError, ValueError, ValidationError) as exc:
            msg = "Unable to obtain old-gateway credentials."
            raise BackendAuthenticationError(msg) from exc


class OldGatewayBackend:
    """Forward compatibility requests to the existing Modal gateway."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        runtime: ObservabilityRuntime | None = None,
    ) -> None:
        """Configure the backend; no HTTP client exists until start is called."""
        self.settings = settings
        self.transport = transport
        self.observability = runtime
        self.client: httpx.AsyncClient | None = None
        self.token_provider: ClientCredentialsTokenProvider | None = None

    async def start(self) -> None:
        """Create the instrumented HTTP client and token provider; idempotent."""
        if self.client is not None:
            return
        timeout = httpx.Timeout(
            connect=self.settings.connect_timeout_seconds,
            read=self.settings.request_timeout_seconds,
            write=self.settings.request_timeout_seconds,
            pool=self.settings.connect_timeout_seconds,
        )
        self.client = httpx.AsyncClient(
            timeout=timeout,
            transport=self.transport,
            follow_redirects=False,
        )
        if self.observability is not None:
            instrument_httpx(self.client, self.observability)
        self.token_provider = ClientCredentialsTokenProvider(
            self.settings,
            self.client,
        )

    async def close(self) -> None:
        """Close the HTTP client; start may be called again afterwards."""
        if self.client is not None:
            await self.client.aclose()
        self.client = None
        self.token_provider = None

    def _runtime(self) -> tuple[httpx.AsyncClient, ClientCredentialsTokenProvider]:
        if self.client is None or self.token_provider is None:
            msg = "Old-gateway backend is not started."
            raise BackendUnavailableError(msg)
        return self.client, self.token_provider

    async def ready(self) -> bool:
        """Return whether the old gateway answers GET /health with 200."""
        try:
            response = await self.request("GET", "/health")
        except (BackendUnavailableError, BackendTimeoutError):
            return False
        return response.status_code == HTTPStatus.OK

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: JsonObject | None = None,
        request_id: str | None = None,
        observability_id: str | None = None,
    ) -> BackendResponse:
        """Forward one request to the old gateway with a service bearer token.

        A connection failure is retried once for GET only. A 401 or 403 refreshes
        the token and replays once for any method, then raises
        BackendAuthenticationError. Raise BackendUnavailableError if the client is
        not started or the gateway is unreachable, and BackendTimeoutError on timeout.
        """
        client, token_provider = self._runtime()
        method = method.upper()
        connection_retries_remaining = 1 if method == "GET" else 0
        auth_refreshes_remaining = 1

        while True:
            token = await token_provider.get_token()
            headers = {"Authorization": f"Bearer {token}"}
            if request_id:
                headers[REQUEST_ID_HEADER] = request_id
            if observability_id:
                headers[OBSERVABILITY_ID_HEADER] = observability_id

            try:
                response = await client.request(
                    method,
                    f"{self.settings.old_gateway_url}{path}",
                    json=json_body,
                    headers=headers,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if connection_retries_remaining:
                    connection_retries_remaining -= 1
                    continue
                msg = "Old gateway is unavailable."
                raise BackendUnavailableError(msg) from exc
            except httpx.TimeoutException as exc:
                msg = "Old gateway timed out."
                raise BackendTimeoutError(msg) from exc
            except httpx.RequestError as exc:
                msg = "Old gateway is unavailable."
                raise BackendUnavailableError(msg) from exc

            if response.status_code in {401, 403}:
                if auth_refreshes_remaining:
                    auth_refreshes_remaining -= 1
                    token_provider.invalidate()
                    # The old gateway rejects invalid bearer credentials with
                    # 403 before endpoint processing. One credential-refresh
                    # replay is therefore safe for POST as well as GET.
                    continue
                msg = "Old gateway rejected refreshed service credentials."
                raise BackendAuthenticationError(msg)

            return BackendResponse(
                status_code=response.status_code,
                content=response.content,
                headers={
                    (
                        REQUEST_ID_HEADER
                        if key.lower() == REQUEST_ID_HEADER.lower()
                        else OBSERVABILITY_ID_HEADER
                        if key.lower() == OBSERVABILITY_ID_HEADER.lower()
                        else key
                    ): value
                    for key, value in response.headers.items()
                    if key.lower() in SAFE_RESPONSE_HEADERS
                },
            )
