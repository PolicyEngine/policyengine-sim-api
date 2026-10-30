"""Decile outputs built from policyengine.py's own decile calculation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fixtures.weighted_frames import weighted_entity_frame
from policyengine.core import Simulation
from policyengine.outputs.decile_impact import calculate_decile_impacts
from policyengine_simulation_executor.simulation_output_distribution import (
    build_decile_output,
)

BASELINE_INCOMES = [10_000.0, 20_000.0, 30_000.0, 40_000.0]


class _InMemorySimulation(Simulation):
    """A simulation whose output is already in memory, so nothing runs."""

    def ensure(self) -> None:
        return None


def _simulation(incomes: list[float]) -> Simulation:
    household = weighted_entity_frame(
        {
            "household_net_income": incomes,
            "household_count_people": [1.0, 2.0, 1.0, 3.0],
            "household_wealth_decile": [1, 1, 2, 2],
            "household_weight": [1.0, 2.0, 3.0, 4.0],
        }
    )
    return _InMemorySimulation.model_construct(
        output_dataset=SimpleNamespace(data=SimpleNamespace(household=household)),
        tax_benefit_model_version=None,
    )


@pytest.mark.parametrize("decile_variable", [None, "household_wealth_decile"])
def test_decile_relative_is_the_fraction_policyengine_computes(decile_variable):
    # Every household gains 1%, so each decile's relative change is 0.01.
    # policyengine.py's relative_change is a percentage (1.0 here); the
    # API's relative field is a fraction. Running policyengine.py's own
    # calculation, not hand-written rows, catches a change of its units.
    impacts = calculate_decile_impacts(
        baseline_simulation=_simulation(BASELINE_INCOMES),
        reform_simulation=_simulation([income * 1.01 for income in BASELINE_INCOMES]),
        decile_variable=decile_variable,
        entity="household",
        quantiles=2,
    )

    output = build_decile_output(impacts)

    rows = impacts.dataframe.set_index("decile")
    assert set(output.relative) == {"1", "2"}
    for decile, relative in output.relative.items():
        row = rows.loc[int(decile)]
        assert relative == pytest.approx(row["absolute_change"] / row["baseline_mean"])
        assert relative == pytest.approx(0.01)
        assert output.average[decile] == pytest.approx(row["absolute_change"])
