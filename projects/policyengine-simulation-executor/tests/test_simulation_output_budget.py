"""Budget totals from weighted output frames, as the worker's simulations hold them."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
from microdf import MicroDataFrame
from policyengine_simulation_executor.simulation_output_budget import (
    build_budgetary_impact,
)


def _simulation(columns: dict[str, list[float]]):
    """A simulation whose household output is a MicroDataFrame, as policyengine builds it."""
    household = MicroDataFrame(pd.DataFrame(columns), weights="household_weight")
    return SimpleNamespace(
        output_dataset=SimpleNamespace(data=SimpleNamespace(household=household))
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
    # The other totals stay weighted sums, Σ weight × value.
    assert budget.baseline_net_income == 500.0
    assert budget.tax_revenue_impact == 25.0
    assert budget.state_tax_revenue_impact == 8.0
