from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from policyengine_simulation_contract.json_types import JsonObject

from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.backend import BackendResponse
from policyengine_simulation_entry.config import Settings


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


@dataclass(frozen=True)
class BackendRequest:
    """One request captured by the fake simulation backend."""

    method: str
    path: str
    json_body: JsonObject | None
    request_id: str | None


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
    ) -> BackendResponse:
        self.requests.append(
            BackendRequest(
                method=method,
                path=path,
                json_body=json_body,
                request_id=request_id,
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
