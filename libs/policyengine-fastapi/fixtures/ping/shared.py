"""Shared fixtures for ping endpoint tests."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from policyengine_fastapi import ping
from policyengine_fastapi.health import HealthRegistry


@pytest.fixture
def health_registry() -> HealthRegistry:
    return HealthRegistry()


@pytest.fixture
def client(health_registry: HealthRegistry) -> TestClient:
    api = FastAPI()
    ping.include_all_routers(api, health_registry)
    return TestClient(api)
