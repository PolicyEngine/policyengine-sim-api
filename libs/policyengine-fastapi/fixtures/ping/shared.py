"""Shared fixtures for ping endpoint tests."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from policyengine_fastapi import ping
from policyengine_fastapi.health import HealthRegistry


@pytest.fixture
def health_registry() -> HealthRegistry:
    """Provide an empty registry; it reports healthy until a test registers a system."""
    return HealthRegistry()


@pytest.fixture
def client(health_registry: HealthRegistry) -> TestClient:
    """Return a client for a bare app serving ping routes from ``health_registry``."""
    api = FastAPI()
    ping.include_all_routers(api, health_registry)
    return TestClient(api)
