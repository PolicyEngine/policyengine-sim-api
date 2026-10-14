"""Budget totals from weighted output frames, as the worker's simulations hold them."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import numpy as np
import pytest
from fixtures.weighted_frames import weighted_entity_frame
from policyengine_simulation_executor.simulation_output_budget import (
    build_budgetary_impact,
)

COMMON_LOGGER = "policyengine_simulation_executor.simulation_output_common"


def _simulation(columns: dict[str, list[float]]):
    """Build a simulation whose household output is a policyengine MicroDataFrame."""
    household = weighted_entity_frame(columns)
    return SimpleNamespace(
        output_dataset=SimpleNamespace(data=SimpleNamespace(household=household))
    )


def _households(weights: list[float], tax: list[float]):
    return _simulation(
        {
            "household_weight": weights,
            "household_net_income": [100.0, 200.0],
            "household_tax": tax,
            "household_benefits": [5.0, 10.0],
            "household_state_income_tax": [2.0, 3.0],
        }
    )


def test_households_counts_each_weight_once_on_weighted_frames():
    baseline = _simulation(
        {
            "household_weight": [1.0, 2.0],
            "household_net_income": [100.0, 200.0],
            "household_tax": [20.0, 40.0],
            "household_benefits": [5.0, 10.0],
            "household_state_income_tax": [2.0, 3.0],
        }
    )
    reform = _simulation(
        {
            "household_weight": [1.0, 2.0],
            "household_net_income": [120.0, 210.0],
            "household_tax": [25.0, 50.0],
            "household_benefits": [4.0, 8.0],
            "household_state_income_tax": [4.0, 6.0],
        }
    )

    budget = build_budgetary_impact("us", baseline, reform)

    # Households weighted 1 and 2 are 3 households, not 1² + 2² = 5.
    assert budget.households == 3.0
    # The other totals stay weighted sums, Σ weight * value.
    assert budget.baseline_net_income == 500.0
    assert budget.tax_revenue_impact == 25.0
    assert budget.state_tax_revenue_impact == 8.0


def _mismatched_weight_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == COMMON_LOGGER
        and record.levelno == logging.WARNING
        and "different weights" in record.getMessage()
    ]


def test_change_totals_warn_when_baseline_and_reform_are_weighted_differently(
    caplog: pytest.LogCaptureFixture,
):
    baseline = _households([1.0, 2.0], tax=[20.0, 40.0])
    reform = _households([2.0, 1.0], tax=[25.0, 50.0])

    with caplog.at_level(logging.WARNING, logger=COMMON_LOGGER):
        budget = build_budgetary_impact("us", baseline, reform)

    # reform - baseline is weighted by the reform's weights alone, and the
    # total is still published.
    expected_change = 2.0 * (25.0 - 20.0) + 1.0 * (50.0 - 40.0)
    assert budget.tax_revenue_impact == pytest.approx(expected_change)
    warnings = _mismatched_weight_warnings(caplog)
    assert warnings
    assert all("household frames" in warning for warning in warnings)


def test_change_totals_accept_weights_after_a_float32_round_trip(
    caplog: pytest.LogCaptureFixture,
):
    weights = [1.1, 2.3]
    stored_weights = np.asarray(weights, dtype=np.float32).astype(float).tolist()
    baseline = _households(weights, tax=[20.0, 40.0])
    reform = _households(stored_weights, tax=[25.0, 50.0])

    with caplog.at_level(logging.WARNING, logger=COMMON_LOGGER):
        budget = build_budgetary_impact("us", baseline, reform)

    assert _mismatched_weight_warnings(caplog) == []

    # The change is weighted by the reform's (round-tripped) weights.
    expected_change = stored_weights[0] * (25.0 - 20.0) + stored_weights[1] * (
        50.0 - 40.0
    )
    assert budget.tax_revenue_impact == pytest.approx(expected_change)
