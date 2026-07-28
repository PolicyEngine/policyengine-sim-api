"""Live authentication checks for a deployed Simulation Entrypoint candidate."""

from __future__ import annotations

import os

import httpx
import pytest


PROTECTED_PATH = "/simulate/economy/comparison"
PROBE_PAYLOAD = {
    "country": "__authentication_probe__",
    "scope": "macro",
    "reform": {},
}


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for authenticated deployment tests.")
    return value


@pytest.fixture(scope="session")
def base_url() -> str:
    return _required("SIMULATION_ENTRYPOINT_TEST_BASE_URL").rstrip("/")


@pytest.fixture(scope="session")
def access_token() -> str:
    issuer = _required("SIMULATION_ENTRYPOINT_TEST_AUTH_ISSUER").rstrip("/")
    response = httpx.post(
        f"{issuer}/oauth/token",
        json={
            "client_id": _required("SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_ID"),
            "client_secret": _required("SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_SECRET"),
            "audience": _required("SIMULATION_ENTRYPOINT_TEST_AUTH_AUDIENCE"),
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Auth0 response omitted access_token.")
    return token


def test_protected_route_rejects_missing_token(base_url: str):
    response = httpx.post(
        f"{base_url}{PROTECTED_PATH}",
        json=PROBE_PAYLOAD,
        timeout=30,
    )

    assert response.status_code == 403


def test_protected_route_rejects_invalid_token(base_url: str):
    response = httpx.post(
        f"{base_url}{PROTECTED_PATH}",
        json=PROBE_PAYLOAD,
        headers={"Authorization": "Bearer not-a-valid-jwt"},
        timeout=30,
    )

    assert response.status_code == 403


def test_valid_token_reaches_authenticated_old_gateway(
    base_url: str,
    access_token: str,
):
    response = httpx.post(
        f"{base_url}{PROTECTED_PATH}",
        json=PROBE_PAYLOAD,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )

    # The deliberately unknown country is rejected before any simulation is
    # submitted. Reaching this 400 proves both authentication hops succeeded.
    assert response.status_code == 400
    assert response.headers["x-policyengine-simulation-backend"] == "old_gateway"
