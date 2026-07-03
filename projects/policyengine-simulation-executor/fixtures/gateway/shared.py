"""Shared fixtures for gateway tests."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from policyengine_simulation_observability.observability import (
    init_simulation_observability,
)
from src.modal.gateway.auth import require_auth
from src.modal.gateway.endpoints import router


def create_gateway_app(*, authenticate: bool = True) -> FastAPI:
    """Create a FastAPI app with the gateway router for testing.

    By default the auth dependency is overridden with a no-op callable so
    individual tests don't need to stage JWT material. Tests that exercise
    the auth failure path can pass ``authenticate=False`` to keep the real
    dependency wired up.
    """
    app = FastAPI(
        title="Test PolicyEngine Simulation API",
        description="Test instance for unit tests",
        version="0.0.1",
    )
    init_simulation_observability(app, service_role="modal_gateway")
    app.include_router(router)
    if authenticate:
        app.dependency_overrides[require_auth] = lambda: None
    return app


@pytest.fixture
def client() -> TestClient:
    """Create a test client for the gateway API."""
    return TestClient(create_gateway_app())
