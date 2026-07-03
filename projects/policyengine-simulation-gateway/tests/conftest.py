"""Root pytest configuration for gateway tests."""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from policyengine_simulation_gateway.testing import create_gateway_app

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

pytest_plugins = (
    "fixtures.gateway_endpoints",
    "fixtures.package_imports",
)


@pytest.fixture
def client() -> TestClient:
    """Create a test client for the gateway API."""
    return TestClient(create_gateway_app())
