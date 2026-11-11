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
    from collections.abc import Awaitable, Callable

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

#: How the body starts when Modal's web layer, not the gateway's code, answers
#: 500 because the gateway's container stopped mid-request, as when Modal
#: preempts it: ``modal-http: internal error: function was terminated by
#: signal``. The gateway's own 500s are a JSON body or Starlette's plain
#: ``Internal Server Error``.
MODAL_INTERNAL_ERROR_PREFIX = b"modal-http: internal error"


class BackendUnavailableError(RuntimeError):
    """The backend could not be reached."""

    #: How many earlier attempts of this read Modal answered for a stopped
    #: gateway before this error ended it.
    interrupted_attempts: int = 0


class BackendTimeoutError(RuntimeError):
    """The backend did not answer within the configured timeout."""

    #: How many earlier attempts of this read Modal answered for a stopped
    #: gateway before this error ended it.
    interrupted_attempts: int = 0


class BackendAuthenticationError(BackendUnavailableError):
    """The service could not authenticate to the backend."""


class BackendInterruptedError(BackendUnavailableError):
    """Modal answered every attempt of a read for a stopped gateway container."""

    def __init__(self, message: str, *, interrupted_attempts: int) -> None:
        """Keep how many attempts of the read Modal answered."""
        super().__init__(message)
        self.interrupted_attempts = interrupted_attempts


@dataclass(frozen=True)
class InterruptedReadRetry:
    """How a GET that Modal answers for a stopped gateway container is sent again.

    Modal answers ``modal-http: internal error`` 500 when the gateway's
    container stops mid-request, as when Modal preempts it. Another gateway
    container, or the one Modal starts in its place, can answer the same read,
    so the read is sent again after each wait in ``waits_seconds``. No attempt
    starts more than ``window_seconds`` after the read began, and a read sent
    again ends within the backend's gateway read timeout (25 seconds by
    default) of when it began, as an uninterrupted read does. That keeps it
    below the Public API's 30-second timeout for its calls to the entry.
    """

    waits_seconds: tuple[float, ...] = (0.5, 1.0)
    window_seconds: float = 5.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic

    def wait_before_next_attempt(
        self, interrupted_attempts: int, elapsed_seconds: float
    ) -> float | None:
        """Return how long to wait before sending the read again, or None to stop.

        ``interrupted_attempts`` counts the attempts Modal has answered so far,
        and ``elapsed_seconds`` the time since the read began.
        """
        if interrupted_attempts > len(self.waits_seconds):
            return None
        wait_seconds = self.waits_seconds[interrupted_attempts - 1]
        if elapsed_seconds + wait_seconds > self.window_seconds:
            return None
        return wait_seconds


def _modal_answered_for_a_stopped_gateway(response: httpx.Response) -> bool:
    """Return whether Modal, not the gateway's code, sent this 500."""
    return (
        response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        and response.content.lstrip().startswith(MODAL_INTERNAL_ERROR_PREFIX)
    )


@dataclass(frozen=True)
class BackendResponse:
    """A backend reply with only the safe-to-forward response headers kept."""

    status_code: int
    content: bytes
    headers: dict[str, str]
    #: How many earlier attempts of this read Modal answered for a stopped
    #: gateway container before this reply came.
    interrupted_attempts: int = 0


def _relayed_response(
    response: httpx.Response, *, interrupted_attempts: int
) -> BackendResponse:
    """Return the gateway's response with only its safe headers kept."""
    return BackendResponse(
        status_code=response.status_code,
        content=response.content,
        interrupted_attempts=interrupted_attempts,
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

        Raise BackendUnavailableError or BackendTimeoutError when no response
        arrives, and BackendInterruptedError, a BackendUnavailableError, when a
        GET's answer came from the platform because the backend stopped.
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
        interrupted_read_retry: InterruptedReadRetry | None = None,
    ) -> None:
        """Configure the backend; no HTTP client exists until start is called."""
        self.settings = settings
        self.transport = transport
        self.observability = runtime
        self.interrupted_read_retry = interrupted_read_retry or InterruptedReadRetry()
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

        When Modal answers a GET with its own 500 because the gateway's container
        stopped, the gateway never read the job, so the GET is sent again as
        ``interrupted_read_retry`` allows, and the first answer from the gateway
        itself is returned. An attempt sent again gets only what is left of the
        gateway read timeout, counted from when the read began, and raises
        BackendTimeoutError when that runs out. Raise BackendInterruptedError
        when Modal answers every attempt. A POST's 500 is returned as it came,
        since the gateway may have started the job. The reply, or the error that
        ends the request, records how many attempts Modal answered.
        """
        method = method.upper()
        read_retry = self.interrupted_read_retry
        started = read_retry.clock()
        interrupted_attempts = 0
        while True:
            # The first attempt keeps the client's own timeouts; an attempt
            # sent again gets only what is left of the read timeout.
            time_left = (
                started + self.settings.request_timeout_seconds - read_retry.clock()
                if interrupted_attempts
                else None
            )
            try:
                async with asyncio.timeout(time_left):
                    response = await self._send(
                        method,
                        path,
                        json_body=json_body,
                        request_id=request_id,
                        observability_id=observability_id,
                    )
            except TimeoutError as exc:
                msg = "Old gateway timed out."
                timed_out = BackendTimeoutError(msg)
                timed_out.interrupted_attempts = interrupted_attempts
                raise timed_out from exc
            except (BackendUnavailableError, BackendTimeoutError) as error:
                error.interrupted_attempts = interrupted_attempts
                raise
            if method != "GET" or not _modal_answered_for_a_stopped_gateway(response):
                return _relayed_response(
                    response, interrupted_attempts=interrupted_attempts
                )
            interrupted_attempts += 1
            wait_seconds = read_retry.wait_before_next_attempt(
                interrupted_attempts, read_retry.clock() - started
            )
            if wait_seconds is None:
                msg = "Old gateway's container stopped before it answered."
                raise BackendInterruptedError(
                    msg, interrupted_attempts=interrupted_attempts
                )
            await read_retry.sleep(wait_seconds)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: JsonObject | None,
        request_id: str | None,
        observability_id: str | None,
    ) -> httpx.Response:
        """Send one attempt of a request and return the gateway's response.

        A connection failure is retried once for GET only, and a 401 or 403
        refreshes the token and replays once; see ``request``.
        """
        client, token_provider = self._runtime()
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

            return response
