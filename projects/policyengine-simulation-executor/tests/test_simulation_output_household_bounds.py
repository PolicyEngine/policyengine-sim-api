"""Plausibility bounds on the published household count."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fixtures.weighted_frames import weighted_entity_frame
from policyengine_simulation_executor.simulation_output_budget import (
    ImplausibleEntityCountError,
    build_budgetary_impact,
)


def _simulation(weights: list[float]):
    household = weighted_entity_frame(
        {
            "household_weight": weights,
            "household_net_income": [100.0, 200.0],
            "household_tax": [20.0, 40.0],
            "household_benefits": [5.0, 10.0],
            "household_state_income_tax": [2.0, 3.0],
        }
    )
    return SimpleNamespace(
        output_dataset=SimpleNamespace(data=SimpleNamespace(household=household))
    )


def test_households_above_the_country_bound_are_refused():
    # 250 million households is more than the US bound of 200 million.
    simulation = _simulation([1.5e8, 1e8])

    with pytest.raises(ImplausibleEntityCountError, match="US household count"):
        build_budgetary_impact("us", simulation, simulation)


def test_household_bounds_are_set_per_country():
    # 70 million households is plausible for the US but not for the UK, whose
    # bound is 60 million.
    simulation = _simulation([4e7, 3e7])

    assert build_budgetary_impact("us", simulation, simulation).households == 7e7
    with pytest.raises(ImplausibleEntityCountError, match="UK household count"):
        build_budgetary_impact("uk", simulation, simulation)


def test_negative_household_counts_are_refused():
    simulation = _simulation([-3.0, 1.0])

    with pytest.raises(ImplausibleEntityCountError, match="household count -2"):
        build_budgetary_impact("us", simulation, simulation)
