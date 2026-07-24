"""Backend protocol and Stage 5 old-gateway implementation."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import httpx

from policyengine_simulation_entry.config import Settings


SAFE_RESPONSE_HEADERS = frozenset(
    {
        "cache-control",
        "content-type",
        "etag",
        "retry-after",
        "x-request-id",
    }
)


class BackendUnavailable(RuntimeError):
    """The backend could not be reached."""


class BackendTimeout(RuntimeError):
    """The backend did not answer within the configured timeout."""


class BackendAuthenticationError(BackendUnavailable):
    """The service could not authenticate to the backend."""


@dataclass(frozen=True)
class BackendResponse:
    status_code: int
    content: bytes
    headers: dict[str, str]


class SimulationBackend(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def ready(self) -> bool: ...

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> BackendResponse: ...


class ClientCredentialsTokenProvider:
    """Fetch and cache the Simulation Entrypoint's old-gateway M2M token."""

    REFRESH_MARGIN_SECONDS = 60

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            now = time.time()
            if (
                self._token is None
                or now >= self._expires_at - self.REFRESH_MARGIN_SECONDS
            ):
                await self._fetch()
            if self._token is None:  # pragma: no cover
                raise BackendAuthenticationError("Auth0 returned no token.")
            return self._token

    def invalidate(self) -> None:
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
            payload = response.json()
            token = payload.get("access_token")
            expires_in = payload.get("expires_in")
            if not token or expires_in is None:
                raise BackendAuthenticationError(
                    "Auth0 response omitted access_token or expires_in."
                )
            self._token = str(token)
            self._expires_at = time.time() + max(int(expires_in), 1)
        except BackendAuthenticationError:
            raise
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise BackendAuthenticationError(
                "Unable to obtain old-gateway credentials."
            ) from exc


class OldGatewayBackend:
    """Forward compatibility requests to the existing Modal gateway."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.transport = transport
        self.client: httpx.AsyncClient | None = None
        self.token_provider: ClientCredentialsTokenProvider | None = None

    async def start(self) -> None:
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
        self.token_provider = ClientCredentialsTokenProvider(
            self.settings,
            self.client,
        )

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
        self.client = None
        self.token_provider = None

    def _runtime(self) -> tuple[httpx.AsyncClient, ClientCredentialsTokenProvider]:
        if self.client is None or self.token_provider is None:
            raise BackendUnavailable("Old-gateway backend is not started.")
        return self.client, self.token_provider

    async def ready(self) -> bool:
        try:
            response = await self.request("GET", "/health")
        except (BackendUnavailable, BackendTimeout):
            return False
        return response.status_code == 200

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> BackendResponse:
        client, token_provider = self._runtime()
        method = method.upper()
        connection_retries_remaining = 1 if method == "GET" else 0
        auth_refreshes_remaining = 1

        while True:
            token = await token_provider.get_token()
            headers = {"Authorization": f"Bearer {token}"}
            if request_id:
                headers["X-Request-ID"] = request_id

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
                raise BackendUnavailable("Old gateway is unavailable.") from exc
            except httpx.TimeoutException as exc:
                raise BackendTimeout("Old gateway timed out.") from exc
            except httpx.RequestError as exc:
                raise BackendUnavailable("Old gateway is unavailable.") from exc

            if response.status_code in {401, 403}:
                if auth_refreshes_remaining:
                    auth_refreshes_remaining -= 1
                    token_provider.invalidate()
                    # The old gateway rejects invalid bearer credentials with
                    # 403 before endpoint processing. One credential-refresh
                    # replay is therefore safe for POST as well as GET.
                    continue
                raise BackendAuthenticationError(
                    "Old gateway rejected refreshed service credentials."
                )

            return BackendResponse(
                status_code=response.status_code,
                content=response.content,
                headers={
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() in SAFE_RESPONSE_HEADERS
                },
            )
