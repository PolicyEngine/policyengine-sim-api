from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi.testclient import TestClient
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.backend import BackendResponse, InterruptedReadRetry
from policyengine_simulation_entry.config import Settings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from policyengine_simulation_contract.json_types import JsonObject


def make_settings(**overrides) -> Settings:
    values = {
        "environment": "test",
        "revision": "simulation-entry-test-revision",
        "auth_required": True,
        "auth_issuer": "https://issuer.example/",
        "auth_audience": "simulation-entry",
        "old_gateway_url": (
            "https://policyengine--policyengine-simulation-gateway-web-app.modal.run"
        ),
        "old_gateway_auth_issuer": "https://issuer.example/",
        "old_gateway_auth_audience": "simulation-api",
        "old_gateway_auth_client_id": "simulation-entry-service",
        "old_gateway_auth_client_secret": "secret",
    }
    values.update(overrides)
    return Settings(**values)


# What Modal's web layer, not the gateway's code, answers when the gateway's
# container stops mid-request, as when Modal preempted it during run
# 42894301943.
MODAL_STOPPED_CONTAINER_BODY = (
    b"modal-http: internal error: function was terminated by signal"
)


@dataclass(frozen=True)
class GatewayReply:
    """One reply the fake gateway gives to a gateway request."""

    status_code: int
    content: bytes
    content_type: str


MODAL_STOPPED_CONTAINER_REPLY = GatewayReply(
    500, MODAL_STOPPED_CONTAINER_BODY, "text/plain; charset=utf-8"
)
RUNNING_JOB_REPLY = GatewayReply(202, b'{"status": "running"}', "application/json")


def gateway_transport_replying_in_turn(
    replies: Sequence[GatewayReply | Exception],
    *,
    gateway_requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """Issue a service token, and give gateway requests ``replies`` in turn.

    The last reply repeats for every later request. An exception in
    ``replies`` is raised instead of a reply. Each gateway request is appended
    to ``gateway_requests`` when it is given.
    """
    replies_given = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal replies_given
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={"access_token": "service-token", "expires_in": 3600},
            )
        if gateway_requests is not None:
            gateway_requests.append(request)
        reply = replies[min(replies_given, len(replies) - 1)]
        replies_given += 1
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(
            reply.status_code,
            content=reply.content,
            headers={"content-type": reply.content_type},
        )

    return httpx.MockTransport(handler)


class RecordedWaits:
    """Stand in for ``asyncio.sleep``: record each wait instead of waiting."""

    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.seconds.append(seconds)

    def retry(self) -> InterruptedReadRetry:
        """Return the default retry of interrupted reads, waiting through ``sleep``."""
        return InterruptedReadRetry(sleep=self.sleep)


@dataclass(frozen=True)
class BackendRequest:
    """One request captured by the fake simulation backend."""

    method: str
    path: str
    json_body: JsonObject | None
    request_id: str | None
    observability_id: str | None


class FakeBackend:
    def __init__(self):
        self.requests: list[BackendRequest] = []
        self.responses: dict[tuple[str, str], BackendResponse] = {}
        self.is_ready = True
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.started = False

    async def ready(self) -> bool:
        return self.is_ready

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: JsonObject | None = None,
        request_id: str | None = None,
        observability_id: str | None = None,
    ) -> BackendResponse:
        self.requests.append(
            BackendRequest(
                method=method,
                path=path,
                json_body=json_body,
                request_id=request_id,
                observability_id=observability_id,
            )
        )
        return self.responses.get(
            (method, path),
            BackendResponse(
                status_code=200,
                content=b"{}",
                headers={"content-type": "application/json"},
            ),
        )


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def client(backend: FakeBackend):
    app = create_app(
        settings=make_settings(),
        backend=backend,
        auth_dependency=lambda: None,
    )
    with TestClient(app) as test_client:
        yield test_client
