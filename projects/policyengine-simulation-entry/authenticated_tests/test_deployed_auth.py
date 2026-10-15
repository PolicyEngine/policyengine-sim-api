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
# A read-only route guarded by stage12:operate, which the deployed test client
# must not hold. The nil identifier never names a real Stage 12 report.
PERMISSION_PROBE_PATH = "/internal/stage12/reports/00000000-0000-0000-0000-000000000000"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        msg = f"{name} is required for authenticated deployment tests."
        raise RuntimeError(msg)
    return value


@pytest.fixture(scope="session")
def base_url() -> str:
    """Return the deployed candidate's base URL, without a trailing slash."""
    return _required("SIMULATION_ENTRYPOINT_TEST_BASE_URL").rstrip("/")


@pytest.fixture(scope="session")
def permission_enforcement() -> None:
    """Skip the test unless the target environment enforces permissions.

    Raise RuntimeError if SIMULATION_ENTRYPOINT_TEST_ENFORCE_PERMISSIONS is set
    to anything other than 0 or 1.
    """
    value = (
        os.environ.get("SIMULATION_ENTRYPOINT_TEST_ENFORCE_PERMISSIONS", "").strip()
        or "0"
    )
    if value not in {"0", "1"}:
        msg = "SIMULATION_ENTRYPOINT_TEST_ENFORCE_PERMISSIONS must be 0 or 1."
        raise RuntimeError(msg)
    if value == "0":
        pytest.skip("The target environment logs missing permissions only.")


@pytest.fixture(scope="session")
def access_token() -> str:
    """Fetch a client-credentials token for the candidate's caller audience.

    Raise RuntimeError if a required variable is unset or Auth0 returns no token.
    """
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
        msg = "Auth0 response omitted access_token."
        raise RuntimeError(msg)
    return token


def test_protected_route_rejects_missing_token(base_url: str) -> None:
    """Check the deployed comparison route returns 403 without a bearer token."""
    response = httpx.post(
        f"{base_url}{PROTECTED_PATH}",
        json=PROBE_PAYLOAD,
        timeout=30,
    )

    assert response.status_code == 403


def test_protected_route_rejects_invalid_token(base_url: str) -> None:
    """Check the deployed comparison route returns 403 for a malformed token."""
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
) -> None:
    """Check a valid token passes both auth hops to reach the old gateway."""
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


@pytest.mark.usefixtures("permission_enforcement")
def test_enforced_permissions_reject_token_without_route_permission(
    base_url: str,
    access_token: str,
) -> None:
    """Check a valid token without stage12:operate gets 403 on a Stage 12 route."""
    response = httpx.get(
        f"{base_url}{PERMISSION_PROBE_PATH}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )

    # The token is valid, so only the missing stage12:operate permission can
    # produce this 403. Any other status means enforcement is off or the test
    # client has been granted stage12:operate.
    assert response.status_code == 403
