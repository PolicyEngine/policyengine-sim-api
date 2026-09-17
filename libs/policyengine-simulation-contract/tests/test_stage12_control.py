"""Tests for binary, fail-closed Stage 12 runtime controls."""

from datetime import datetime, timezone

from policyengine_simulation_contract.stage12_control import (
    ACTIVE_CONTROL_KEY,
    Stage12DualExecutionControl,
    Stage12DualExecutionControlLoader,
    publish_control,
)

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def _control(**updates):
    values = {
        "environment": "staging",
        "manifest_selection_enabled": True,
        "economy_enabled": True,
        "updated_at": NOW,
    }
    values.update(updates)
    return Stage12DualExecutionControl(**values)


def test_dual_execution_requires_both_binary_controls() -> None:
    store = {}
    loader = Stage12DualExecutionControlLoader(store)

    publish_control(store, _control())
    assert loader.enabled(calculation_flow="economy", environment="staging")

    publish_control(store, _control(economy_enabled=False))
    assert not loader.enabled(calculation_flow="economy", environment="staging")

    publish_control(store, _control(manifest_selection_enabled=False))
    assert not loader.enabled(calculation_flow="economy", environment="staging")


def test_control_fails_closed_for_missing_invalid_or_wrong_environment() -> None:
    store = {}
    loader = Stage12DualExecutionControlLoader(store)

    assert not loader.enabled(calculation_flow="economy", environment="staging")
    store[ACTIVE_CONTROL_KEY] = {"invalid": True}
    assert not loader.enabled(calculation_flow="economy", environment="staging")
    publish_control(store, _control(environment="production"))
    assert not loader.enabled(calculation_flow="economy", environment="staging")
    assert not loader.enabled(calculation_flow="household", environment="production")
