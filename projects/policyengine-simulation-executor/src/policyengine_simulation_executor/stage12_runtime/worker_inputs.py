"""A simulation worker's inputs: the parameters it calculates from, and its context.

Both the comparison path's worker and a detailed-save worker, and the
coordinator reading a reused simulation run's output, use these, so they
live apart from the worker's own modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_bundle import CountryId
    from policyengine_simulation_contract.stage12_execution import (
        PlannedSimulationExecutionInput,
        SimulationExecutionInput,
        Stage12InvocationContext,
    )


def simulation_runtime_params(
    simulation: SimulationExecutionInput,
) -> dict[str, Any]:
    """Return the parameters a Stage 12 simulation is resolved and built from."""
    return {
        "country": simulation.geography.country,
        "scope": "macro",
        "time_period": str(simulation.year),
        "region": simulation.geography.region,
        **simulation.options,
    }


def require_worker_context(
    simulation: PlannedSimulationExecutionInput,
    context: Stage12InvocationContext,
    *,
    required_country: CountryId,
) -> None:
    """Raise ValueError unless ``context`` describes this worker and simulation.

    The simulation must be for this worker's country, on the context's
    worker version and bundle, under an artifact prefix naming its
    evaluation, with an output plan for the same country.
    """
    if simulation.geography.country != required_country:
        msg = "single-simulation input was sent to another country worker"
        raise ValueError(msg)
    if simulation.bundle.policyengine_version != context.worker_version:
        msg = "single-simulation worker version does not match its context"
        raise ValueError(msg)
    if simulation.bundle.bundle_manifest_sha256 != context.bundle_manifest_sha256:
        msg = "single-simulation bundle digest does not match its context"
        raise ValueError(msg)
    if str(simulation.evaluation_id) not in context.artifact_prefix:
        msg = "single-simulation artifact prefix names another run"
        raise ValueError(msg)
    if simulation.output_plan.country != required_country:
        msg = "single-simulation output plan names another country"
        raise ValueError(msg)
