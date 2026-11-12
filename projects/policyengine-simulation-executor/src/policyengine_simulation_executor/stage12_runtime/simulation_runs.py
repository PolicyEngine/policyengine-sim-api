"""A worker's detailed-save call: claim its simulation run, calculate, record.

The call's context names one simulation run of the Public API's run tables.
A report run's coordinator starts it with a planned simulation; the
simulation service may start it on a simulation run of its own, which the
worker plans by itself (``resolve_simulation_output_plan``). The worker
claims the run under its own Modal call ID, so Modal restarting a
pre-empted call resumes it, and a run another call holds, or one that has
finished, is not calculated again. It records the run's output file, or its
failure, only while its call still holds the run, and writes no evaluation
record.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.stage12_execution import (
    PlannedSimulationExecutionInput,
    SimulationExecutionInput,
)
from policyengine_simulation_observability.stages import (
    STAGE12_SIMULATION_STAGES,
    Stage,
)
from policyengine_stage12_persistence import RunStatus

from .failure_causes import (
    StageProgress,
    StageTracking,
    record_failure_cause,
    tracked_stage,
)
from .failure_recording import FailureSite, persist_failure_record, report_failure
from .failures import (
    Stage12CancellationError,
    Stage12ExecutionError,
    Stage12InputError,
    validate_policy_periods,
)
from .output_planning import plan_simulation_input, validate_output_frames
from .reused_outputs import checked_output
from .worker_inputs import require_worker_context

if TYPE_CHECKING:
    from collections.abc import Callable

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.stage12_bundle import CountryId
    from policyengine_simulation_contract.stage12_execution import (
        SimulationArtifactDescriptor,
        Stage12InvocationContext,
        Stage12OutputPlan,
    )

    from policyengine_simulation_executor.stage12_artifacts import (
        Stage12ArtifactStore,
    )

    from .report_run_recording import SimulationRunRecorder
    from .reused_outputs import OutputFileReader
    from .simulation import SimulationCalculation

    # Calculates a planned simulation, given its stage progress, its stage
    # spans and a check of whether it was stopped.
    PlannedCalculation = Callable[..., SimulationCalculation]

logger = logging.getLogger(__name__)

# The worker's input names another simulation run than its call claimed.
SIMULATION_RUN_INPUT_MISMATCH = "simulation_run_input_mismatch"


def run_simulation_run(  # noqa: PLR0913  # the call's inputs, then the worker's seams
    payload: object,
    context: Stage12InvocationContext,
    *,
    required_country: CountryId,
    recorder: SimulationRunRecorder,
    artifact_storage: Stage12ArtifactStore,
    planner: Callable[[SimulationExecutionInput], Stage12OutputPlan],
    calculate: PlannedCalculation,
    output_files: OutputFileReader,
    runtime: ObservabilityRuntime | None,
) -> dict[str, Any]:
    """Claim the simulation run, calculate it, and record its output file.

    Returns the output's ``SimulationArtifactDescriptor`` as JSON. A run that
    already succeeded returns its stored output's descriptor without
    calculating, as when Modal restarts a call that had completed its run;
    a run another call holds, or one that failed, returns
    ``{"deduplicated": True, "simulation_run_id", "status"}``. Once the call
    holds the run, a failure, of the input or the calculation, fails the run
    and raises a ``RuntimeError`` carrying no calculation detail; a run
    failed or released meanwhile records nothing more and raises one that
    says it was cancelled. A pre-emption propagates, since Modal runs the
    call again; so does Modal's cancellation, and the stuck-run sweep fails
    the run once Modal reports the call ended.
    """
    claimed = recorder.claim()
    if not claimed.changed:
        if claimed.status is RunStatus.SUCCEEDED:
            simulation = _planned_simulation(payload, planner)
            return checked_output(
                simulation,
                recorder.simulation_run_id,
                recorder.state(),
                files=output_files,
            ).descriptor.model_dump(mode="json")
        return {
            "deduplicated": True,
            "simulation_run_id": str(recorder.simulation_run_id),
            "status": claimed.status.value,
        }
    progress = StageProgress(role=_role(payload))
    stage_span = partial(
        tracked_stage,
        tracking=StageTracking(STAGE12_SIMULATION_STAGES, progress, runtime),
    )
    try:
        simulation = _checked_simulation(
            payload,
            context,
            recorder=recorder,
            planner=planner,
            required_country=required_country,
        )
        with stage_span(Stage.STAGE12_INPUT_WRITE):
            artifact_storage.write_input(
                prefix=context.artifact_prefix, simulation=simulation
            )
        calculation = calculate(
            simulation,
            progress=progress,
            stage_span=stage_span,
            cancellation_requested=partial(_run_taken_from_call, recorder),
        )
        validate_output_frames(calculation.frames, simulation.output_plan)
        with stage_span(Stage.STAGE12_SIMULATION_ARTIFACT_WRITE):
            descriptor = artifact_storage.write_simulation_output(
                context,
                simulation=simulation,
                frames=calculation.frames,
                calculation_provenance=calculation.calculation_provenance,
                uk_local_authority_metadata=calculation.uk_local_authority_metadata,
            )
        _require_completed(recorder, descriptor)
        return descriptor.model_dump(mode="json")
    except Stage12CancellationError:
        msg = "Stage 12 simulation was cancelled"
        raise RuntimeError(msg) from None
    # Persist a redacted cause for any input or calculation failure before
    # returning a stable exception to Modal.
    except Exception as error:  # noqa: BLE001  # every failure is recorded before a stable exception returns to Modal
        _record_failure(error, recorder, runtime=runtime, progress=progress)
        msg = "Stage 12 simulation execution failed"
        raise RuntimeError(msg) from None


def _checked_simulation(
    payload: object,
    context: Stage12InvocationContext,
    *,
    recorder: SimulationRunRecorder,
    planner: Callable[[SimulationExecutionInput], Stage12OutputPlan],
    required_country: CountryId,
) -> PlannedSimulationExecutionInput:
    """Return the planned simulation, checked against the run the call claimed.

    Raises ``Stage12InputError`` for an input naming another simulation run,
    or a policy with an invalid period, and ValueError for a context
    describing another worker.
    """
    simulation = _planned_simulation(payload, planner)
    require_worker_context(simulation, context, required_country=required_country)
    if simulation.simulation_execution_id != recorder.simulation_run_id:
        raise Stage12InputError(
            SIMULATION_RUN_INPUT_MISMATCH,
            "The simulation input's simulation_execution_id is "
            f"{simulation.simulation_execution_id}, not its run's "
            f"{recorder.simulation_run_id}.",
        )
    validate_policy_periods(simulation.policy)
    return simulation


def _require_completed(
    recorder: SimulationRunRecorder, descriptor: SimulationArtifactDescriptor
) -> None:
    """Record the output file; raise if the run was taken from this call meanwhile.

    The coordinator fails or releases a run it stops, and the write then
    changes nothing: ``Stage12CancellationError``.
    """
    if not recorder.complete(descriptor).changed:
        msg = "Stage 12 simulation was cancelled"
        raise Stage12CancellationError(msg)


def _planned_simulation(
    payload: object,
    planner: Callable[[SimulationExecutionInput], Stage12OutputPlan],
) -> PlannedSimulationExecutionInput:
    """Return the planned simulation; plan one given without an output plan."""
    if isinstance(payload, dict) and "output_plan" in payload:
        return PlannedSimulationExecutionInput.model_validate(payload)
    simulation = SimulationExecutionInput.model_validate(payload)
    return plan_simulation_input(simulation, planner(simulation))


def _role(payload: object) -> str | None:
    """Return the role a payload names, for stage tracking, before it is checked."""
    role = payload.get("role") if isinstance(payload, dict) else None
    return role if isinstance(role, str) else None


def _run_taken_from_call(recorder: SimulationRunRecorder) -> bool:
    """Return whether the run is no longer running under this call.

    A segmented calculation checks it between polls, and stops its
    region-group calls once the coordinator failed or released the run. A
    failed read is logged and treated as not stopped.
    """
    try:
        return not recorder.still_held()
    except Exception as error:  # noqa: BLE001  # a failed read must not stop the calculation
        logger.warning(
            "Could not read simulation run %s (%s)",
            recorder.simulation_run_id,
            type(error).__name__,
        )
        return False


def _record_failure(
    error: Exception,
    recorder: SimulationRunRecorder,
    *,
    runtime: ObservabilityRuntime | None,
    progress: StageProgress,
) -> None:
    """Report a failure once, then fail the run while this call holds it."""
    if isinstance(error, Stage12ExecutionError):
        # A segmented calculation reports its segment failure where it
        # occurs, before raising this error.
        detail = error.detail
    else:
        detail = report_failure(
            error,
            FailureSite(
                scope="simulation_execution",
                default_code="simulation_execution_failed",
                attributes={
                    "simulation_role": progress.process_role,
                    "simulation_run_id": str(recorder.simulation_run_id),
                },
            ),
            runtime=runtime,
            progress=progress,
        )
    record_failure_cause(runtime, error, detail.error_summary)
    persist_failure_record(
        partial(recorder.fail, detail),
        runtime=runtime,
        failure_scope="simulation_execution",
    )
