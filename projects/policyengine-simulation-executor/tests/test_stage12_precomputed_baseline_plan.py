"""The precomputed US baselines are planned as a simulation run stores its output.

A report run's coordinator plans each simulation run with the plan its
simulation alone decides (``resolve_simulation_output_plan``), and a worker
looks a current-law baseline up in the precomputed cache under that plan's
digest. The precompute must plan its baselines the same way, or no report
run's US national baseline finds its precomputed file.
"""

from __future__ import annotations

import pytest
from policyengine_simulation_contract.stage12_execution import (
    SimulationExecutionInput,
    stage12_output_plan_sha256,
)
from policyengine_simulation_executor.stage12_cache import precompute_planning
from policyengine_simulation_executor.stage12_runtime.output_planning import (
    resolve_simulation_output_plan,
)
from policyengine_simulation_executor.stage12_runtime.partition import US_REGION_GROUPS


@pytest.mark.parametrize("reform", [False, True])
def test_a_precomputed_simulation_is_planned_as_its_simulation_run_stores_it(
    reform: bool,  # noqa: FBT001  # the parametrized side
) -> None:
    planned = precompute_planning.scoped_planned_simulation(
        precompute_planning.PRECOMPUTE_YEARS[0], US_REGION_GROUPS[0], reform=reform
    )
    unplanned = SimulationExecutionInput.model_validate(
        planned.model_dump(mode="json", exclude={"output_plan"})
    )

    stored_plan = resolve_simulation_output_plan(unplanned)

    assert stage12_output_plan_sha256(planned.output_plan) == (
        stage12_output_plan_sha256(stored_plan)
    )
