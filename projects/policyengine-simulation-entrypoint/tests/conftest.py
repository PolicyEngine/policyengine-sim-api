from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient

from policyengine_simulation_entrypoint.app import create_app
from policyengine_simulation_entrypoint.backend import BackendResponse
from policyengine_simulation_entrypoint.config import Settings


def make_settings(**overrides) -> Settings:
    values = {
        "environment": "test",
        "public_url": "https://simulation.example.test",
        "auth_required": True,
        "auth_issuer": "https://issuer.example/",
        "auth_audience": "simulation-entrypoint",
        "old_gateway_url": "https://old-gateway.example.test",
        "old_gateway_auth_issuer": "https://issuer.example/",
        "old_gateway_auth_audience": "simulation-api",
        "old_gateway_auth_client_id": "simulation-entrypoint-service",
        "old_gateway_auth_client_secret": "secret",
    }
    values.update(overrides)
    return Settings(**values)


class FakeBackend:
    def __init__(self):
        self.requests: list[dict[str, Any]] = []
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
        json_body: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> BackendResponse:
        self.requests.append(
            {
                "method": method,
                "path": path,
                "json_body": json_body,
                "request_id": request_id,
            }
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
