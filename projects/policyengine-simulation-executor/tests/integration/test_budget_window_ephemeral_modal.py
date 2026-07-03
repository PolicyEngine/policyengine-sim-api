"""Real-Modal integration smoke test for the budget-window batch path.

This test is intentionally skipped by default. It is meant to be run
against an ephemeral Modal environment (``modal environment create``) that
has the gateway and a versioned worker app deployed; it exercises the
full spawn + poll loop against the real control plane.

Run explicitly with::

    pytest tests/integration -m integration

The test body is a skeleton; a follow-up change will wire up the actual
ephemeral deployment fixtures once the orchestration scripts land.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def modal_integration_environment():
    """Guard the integration test behind an explicit env opt-in."""
    if not os.environ.get("POLICYENGINE_MODAL_INTEGRATION_BASE_URL"):
        pytest.skip(
            "Set POLICYENGINE_MODAL_INTEGRATION_BASE_URL to enable the "
            "Modal integration smoke test."
        )
    return os.environ["POLICYENGINE_MODAL_INTEGRATION_BASE_URL"]


def test_budget_window_submission_reaches_complete(
    modal_integration_environment: str,
):
    """Placeholder for the real integration run.

    The real implementation will submit a 2-year US batch to the ephemeral
    gateway URL, poll until ``status=="complete"``, and assert the
    returned annual impacts match a golden fixture. For now the test is
    intentionally empty so the integration bucket exists as a real
    directory with a valid Python test module.
    """

    assert modal_integration_environment.startswith("http")
