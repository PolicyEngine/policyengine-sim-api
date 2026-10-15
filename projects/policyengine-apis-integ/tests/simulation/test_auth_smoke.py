"""End-to-end auth smoke tests for the simulation gateway."""

from __future__ import annotations

import httpx
import pytest

from .conftest import settings

pytestmark = pytest.mark.skipif(
    not settings.gateway_auth_required,
    reason="Gateway auth is not required for this deployment; skipping auth smoke.",
)


def _base() -> str:
    return settings.base_url.rstrip("/")


def test_gated_endpoint_rejects_missing_token() -> None:
    """Unauthenticated access to a gated endpoint must be rejected."""
    response = httpx.get(
        f"{_base()}/jobs/auth-smoke-probe-no-token",
        timeout=30.0,
    )

    assert response.status_code in (401, 403), (
        f"Expected /jobs to reject an unauthenticated request with 401/403, "
        f"got {response.status_code}: {response.text[:200]}"
    )


def test_gated_endpoint_accepts_valid_token() -> None:
    """A valid token must advance past auth and reach the job lookup."""
    assert settings.access_token, (
        "simulation_integ_test_access_token must be set when gateway auth is required"
    )

    response = httpx.get(
        f"{_base()}/jobs/auth-smoke-probe-does-not-exist",
        headers={"Authorization": f"Bearer {settings.access_token}"},
        timeout=30.0,
    )

    auth_failures = {401, 403, 503}
    assert response.status_code not in auth_failures, (
        f"Gated endpoint rejected a valid token with {response.status_code}: "
        f"{response.text[:200]}"
    )
    assert response.status_code == 404, (
        f"Expected 404 for an unknown job id after auth, got "
        f"{response.status_code}: {response.text[:200]}"
    )
