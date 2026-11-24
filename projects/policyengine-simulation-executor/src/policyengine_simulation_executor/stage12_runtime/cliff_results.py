"""A worker's cliff calculation of one simulation run: claim, calculate, record.

Each simulation run can have a cliff result, its cliff gap and cliff share,
which every report naming the run reads. A simulation worker's call whose
context names a cliff result claims it under the call's Modal call ID,
checks that the payload is the simulation of the cliff result's run on the
run's year and versions, calculates that simulation with the cliff columns,
and records the result or its failure, only while its call still holds it.
A cliff result another call holds, or one that finished, is not calculated
again. The call writes nothing to the simulation run, its output file or any
report run.

The cliff gap and share are .py's: ``cliff_gap`` reads marginal tax rates,
which recalculate each household's net income with extra earnings, so the
simulation is calculated again from its dataset and policy; the run's stored
output file has no cliff columns. The gap is the weighted sum of
``cliff_gap``, and the share the weighted count of people ``is_on_cliff``
over the weighted count of ``is_adult`` (``policyengine.outputs.cliff_impact``).
A US national calculation runs as the region groups a plain run uses: each
person's cliff columns depend only on their own household, each group keeps
its households' weights, and the share is taken once, from the merged rows.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

import pandas as pd
from microdf import MicroDataFrame
from policyengine.outputs.cliff_impact import CLIFF_IMPACT_VARIABLES
from policyengine_simulation_contract.stage12_execution import (
    SimulationExecutionInput,
)
from policyengine_simulation_observability.stages import STAGE12_SIMULATION_STAGES
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
from .output_planning import (
    plan_simulation_input,
    resolve_cliff_output_plan,
    validate_output_frames,
)
from .partition import should_segment_cliff_calculation
from .report_run_access import cliff_result_store
from .report_run_recording import run_error_message
from .worker_inputs import require_worker_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from contextlib import AbstractContextManager
    from uuid import UUID

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.stage12_bundle import CountryId
    from policyengine_simulation_contract.stage12_execution import (
        PlannedSimulationExecutionInput,
        Stage12InvocationContext,
        Stage12OutputPlan,
    )
    from policyengine_simulation_observability.stages import Stage
    from policyengine_stage12_persistence import (
        CliffResultState,
        RunWrite,
        SimulationRunState,
    )

    from .failures import Stage12FailureDetail
    from .simulation import SimulationCalculation, SimulationWorkerDependencies

logger = logging.getLogger(__name__)

# The payload is not the simulation of the cliff result's run, on its year
# and versions.
CLIFF_RESULT_INPUT_MISMATCH = "cliff_result_input_mismatch"
# The calculation found no adults, or a total that is not a finite number,
# so the cliff share has no value.
CLIFF_SHARE_UNDEFINED = "cliff_share_undefined"
# The column the cliff share divides by.
ADULT_COUNT_VARIABLE = "is_adult"


class CliffResultWriter(Protocol):
    """Cliff result writes and the reads they check; ``CliffResultStore`` implements it.

    Each write is conditional on the cliff result's status and returns
    whether it changed it, and its status afterwards.
    """

    def get_cliff_result(self, cliff_result_id: UUID) -> CliffResultState:
        """Return a cliff result's state; LookupError when it does not exist."""
        ...

    def get_simulation_run(self, simulation_run_id: UUID) -> SimulationRunState:
        """Return a simulation run's state; LookupError when it does not exist."""
        ...

    def claim_cliff_result(
        self, cliff_result_id: UUID, *, modal_call_id: str, claimed_at: datetime
    ) -> RunWrite:
        """Mark a pending or running cliff result that records no other call running."""
        ...

    def complete_cliff_result(
        self,
        cliff_result_id: UUID,
        *,
        modal_call_id: str,
        cliff_gap: float,
        cliff_share: float,
        completed_at: datetime,
    ) -> RunWrite:
        """Record the values and mark it succeeded while ``modal_call_id`` holds it."""
        ...

    def fail_cliff_result(
        self,
        cliff_result_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: datetime,
    ) -> RunWrite:
        """Mark the cliff result failed while ``modal_call_id`` holds it running."""
        ...


@dataclass(frozen=True)
class CliffTotals:
    """One simulation's calculated cliff gap and cliff share."""

    cliff_gap: float
    cliff_share: float


@dataclass(frozen=True)
class CliffResultRecorder:
    """A worker's writes to the cliff result its call claims, under its call ID."""

    writer: CliffResultWriter
    cliff_result_id: UUID
    modal_call_id: str

    def claim(self) -> RunWrite:
        """Mark the cliff result running under this call, unless another holds it."""
        return self.writer.claim_cliff_result(
            self.cliff_result_id,
            modal_call_id=self.modal_call_id,
            claimed_at=datetime.now(UTC),
        )

    def state(self) -> CliffResultState:
        """Return the cliff result's state as it is now."""
        return self.writer.get_cliff_result(self.cliff_result_id)

    def still_held(self) -> bool:
        """Return whether this call still holds the cliff result running."""
        state = self.state()
        return (
            state.status is RunStatus.RUNNING
            and state.modal_call_id == self.modal_call_id
        )

    def complete(self, totals: CliffTotals) -> RunWrite:
        """Record the cliff gap and share; mark the cliff result succeeded."""
        return self.writer.complete_cliff_result(
            self.cliff_result_id,
            modal_call_id=self.modal_call_id,
            cliff_gap=totals.cliff_gap,
            cliff_share=totals.cliff_share,
            completed_at=datetime.now(UTC),
        )

    def fail(self, detail: Stage12FailureDetail) -> RunWrite:
        """Mark the cliff result failed with ``detail`` while this call holds it."""
        return self.writer.fail_cliff_result(
            self.cliff_result_id,
            modal_call_id=self.modal_call_id,
            error_message=run_error_message(detail.error_code, detail.error_summary),
            failed_at=datetime.now(UTC),
        )


class StandardCalculation(Protocol):
    """Calculates a planned simulation in one worker; ``simulation._calculate``."""

    def __call__(
        self,
        simulation: PlannedSimulationExecutionInput,
        dependencies: SimulationWorkerDependencies,
        *,
        progress: StageProgress,
        stage_span: Callable[[Stage], AbstractContextManager[object]],
        cancellation_requested: Callable[[], bool],
    ) -> SimulationCalculation:
        """Return the calculation, timing each stage with ``stage_span``."""
        ...


def run_named_cliff_result(  # noqa: PLR0913  # the call's inputs, then the worker's seams
    payload: object,
    context: Stage12InvocationContext,
    *,
    required_country: CountryId,
    dependencies: SimulationWorkerDependencies,
    calculate: StandardCalculation,
    modal_call_id: str | None,
) -> dict[str, Any]:
    """Run a call on the cliff result its context names.

    Raises ValueError, before any write, for a context naming no cliff
    result, or a call without its Modal call ID. The store is opened before
    the claim, so a missing setting claims nothing. Returns what
    ``run_cliff_result`` returns.
    """
    cliff_result_id = context.cliff_result_id
    if cliff_result_id is None:
        msg = "a cliff calculation's call names its cliff result"
        raise ValueError(msg)
    if modal_call_id is None:
        msg = "a cliff calculation needs its Modal call ID"
        raise ValueError(msg)
    runtime = dependencies.runtime
    recorder = CliffResultRecorder(
        writer=dependencies.cliff_results or cliff_result_store(runtime),
        cliff_result_id=cliff_result_id,
        modal_call_id=modal_call_id,
    )
    return run_cliff_result(
        payload,
        context,
        required_country=required_country,
        recorder=recorder,
        dependencies=dependencies,
        calculate=calculate,
    )


def run_cliff_result(  # noqa: PLR0913  # the call's inputs, then the worker's seams
    payload: object,
    context: Stage12InvocationContext,
    *,
    required_country: CountryId,
    recorder: CliffResultRecorder,
    dependencies: SimulationWorkerDependencies,
    calculate: StandardCalculation,
) -> dict[str, Any]:
    """Claim the cliff result, calculate its run's cliffs, and record them.

    Returns ``{"cliff_result_id", "simulation_run_id", "status":
    "succeeded", "cliff_gap", "cliff_share"}``, also without calculating for
    a cliff result this call already completed, as when Modal restarts it. A
    cliff result another call holds, or one that failed or that another call
    completed, returns ``{"deduplicated": True, "cliff_result_id",
    "status"}``. Once the call holds it, a failure, of the input or the
    calculation, fails the cliff result and raises a ``RuntimeError``
    carrying no calculation detail; one failed meanwhile, as the stuck-run
    sweep fails it, records nothing more and raises one that says it was
    cancelled. A pre-emption propagates, since Modal runs the call again.
    """
    runtime = dependencies.runtime
    claimed = recorder.claim()
    if not claimed.changed:
        state = recorder.state()
        if (
            claimed.status is RunStatus.SUCCEEDED
            and state.modal_call_id == recorder.modal_call_id
        ):
            return _succeeded(recorder.cliff_result_id, state)
        return {
            "deduplicated": True,
            "cliff_result_id": str(recorder.cliff_result_id),
            "status": claimed.status.value,
        }
    progress = StageProgress(role=_role(payload))
    stage_span = partial(
        tracked_stage,
        tracking=StageTracking(STAGE12_SIMULATION_STAGES, progress, runtime),
    )
    try:
        simulation = _checked_simulation(
            payload, context, recorder=recorder, required_country=required_country
        )
        calculation = _calculation(
            simulation,
            dependencies,
            calculate=calculate,
            progress=progress,
            stage_span=stage_span,
            cancellation_requested=partial(_taken_from_call, recorder),
        )
        validate_output_frames(calculation.frames, simulation.output_plan)
        totals = cliff_totals_from_frames(calculation.frames, simulation.output_plan)
        _require_completed(recorder, totals)
    except Stage12CancellationError:
        msg = "Stage 12 cliff calculation was cancelled"
        raise RuntimeError(msg) from None
    # Persist a redacted cause for any input or calculation failure before
    # returning a stable exception to Modal.
    except Exception as error:  # noqa: BLE001  # every failure is recorded before a stable exception returns to Modal
        _record_failure(error, recorder, runtime=runtime, progress=progress)
        msg = "Stage 12 cliff calculation failed"
        raise RuntimeError(msg) from None
    return {
        "cliff_result_id": str(recorder.cliff_result_id),
        "simulation_run_id": str(simulation.simulation_execution_id),
        "status": RunStatus.SUCCEEDED.value,
        "cliff_gap": totals.cliff_gap,
        "cliff_share": totals.cliff_share,
    }


def _calculation(  # noqa: PLR0913  # the simulation, then the worker's seams
    simulation: PlannedSimulationExecutionInput,
    dependencies: SimulationWorkerDependencies,
    *,
    calculate: StandardCalculation,
    progress: StageProgress,
    stage_span: Callable[[Stage], AbstractContextManager[object]],
    cancellation_requested: Callable[[], bool],
) -> SimulationCalculation:
    """Calculate the simulation as US region groups when it may be, else in one worker.

    ``cancellation_requested`` says whether the cliff result was taken from
    the call, which a segmented calculation checks between polls.
    """
    if dependencies.segmented_calculator is not None and (
        should_segment_cliff_calculation(simulation)
    ):
        return dependencies.segmented_calculator(simulation, cancellation_requested)
    return calculate(
        simulation,
        dependencies,
        progress=progress,
        stage_span=stage_span,
        cancellation_requested=cancellation_requested,
    )


def _require_completed(recorder: CliffResultRecorder, totals: CliffTotals) -> None:
    """Record the values; raise if the cliff result was taken from this call meanwhile.

    One failed meanwhile, as the stuck-run sweep fails one, takes no write
    from it: ``Stage12CancellationError``.
    """
    if not recorder.complete(totals).changed:
        msg = "Stage 12 cliff calculation was cancelled"
        raise Stage12CancellationError(msg)


def cliff_totals_from_frames(
    frames: Mapping[str, pd.DataFrame], output_plan: Stage12OutputPlan
) -> CliffTotals:
    """Return the cliff gap and share of a calculation's weighted output rows.

    Each of .py's cliff variables is summed on the entity the plan
    materializes it on, weighted by that entity's weight, as .py's own
    cliff impact sums them. Raises ``Stage12InputError``
    (``cliff_share_undefined``) when there are no adults, or a total is not
    a finite number.
    """
    totals = {
        variable: _weighted_total(frames, output_plan, variable)
        for variable in CLIFF_IMPACT_VARIABLES
    }
    adults = totals[ADULT_COUNT_VARIABLE]
    if not all(math.isfinite(total) for total in totals.values()) or adults <= 0:
        raise Stage12InputError(
            CLIFF_SHARE_UNDEFINED,
            "The calculation has no adults, or a cliff total that is not a "
            "finite number, so its cliff share has no value.",
        )
    return CliffTotals(
        cliff_gap=totals["cliff_gap"],
        cliff_share=totals["is_on_cliff"] / adults,
    )


def _weighted_total(
    frames: Mapping[str, pd.DataFrame], output_plan: Stage12OutputPlan, variable: str
) -> float:
    """Return the weighted sum of ``variable`` on the entity the plan puts it on."""
    entity = next(
        plan.entity
        for plan in output_plan.entities
        if variable in plan.materialized_variables
    )
    weighted = MicroDataFrame(frames[entity], weights=f"{entity}_weight")
    column = weighted[variable]
    if not isinstance(column, pd.Series):
        msg = f"{entity} output has more than one {variable!r} column"
        raise TypeError(msg)
    return float(column.sum())


def _checked_simulation(
    payload: object,
    context: Stage12InvocationContext,
    *,
    recorder: CliffResultRecorder,
    required_country: CountryId,
) -> PlannedSimulationExecutionInput:
    """Return the simulation planned with the cliff columns, checked against its run.

    Raises ``Stage12InputError`` (``cliff_result_input_mismatch``) for a
    payload that is not the simulation of the cliff result's run, on the
    run's year and versions, ``Stage12InputError`` for a policy with an
    invalid period, and ValueError for a context describing another worker.
    The calculation then checks the run's versions are the installed ones.
    """
    simulation = SimulationExecutionInput.model_validate(_without_output_plan(payload))
    simulation_run_id = recorder.state().simulation_run_id
    run = recorder.writer.get_simulation_run(simulation_run_id)
    calculated = {
        "simulation run": simulation.simulation_execution_id,
        "year": simulation.year,
        "PolicyEngine.py version": simulation.bundle.policyengine_version,
        "country package version": simulation.bundle.country_package_version,
    }
    recorded = {
        "simulation run": simulation_run_id,
        "year": run.year,
        "PolicyEngine.py version": run.policyengine_version,
        "country package version": run.country_package_version,
    }
    differences = [name for name in calculated if calculated[name] != recorded[name]]
    if differences:
        raise Stage12InputError(
            CLIFF_RESULT_INPUT_MISMATCH,
            "The payload is not the simulation of the cliff result's run: its "
            + ", ".join(differences)
            + " differ.",
        )
    planned = plan_simulation_input(simulation, resolve_cliff_output_plan(simulation))
    require_worker_context(planned, context, required_country=required_country)
    validate_policy_periods(planned.policy)
    return planned


def _without_output_plan(payload: object) -> object:
    """Return ``payload`` without an output plan: the cliff calculation plans one."""
    if isinstance(payload, dict) and "output_plan" in payload:
        return {key: value for key, value in payload.items() if key != "output_plan"}
    return payload


def _succeeded(cliff_result_id: UUID, state: CliffResultState) -> dict[str, Any]:
    """Return what a call returns for a cliff result it completed."""
    return {
        "cliff_result_id": str(cliff_result_id),
        "simulation_run_id": str(state.simulation_run_id),
        "status": RunStatus.SUCCEEDED.value,
        "cliff_gap": state.cliff_gap,
        "cliff_share": state.cliff_share,
    }


def _role(payload: object) -> str | None:
    """Return the role a payload names, for stage tracking, before it is checked."""
    role = payload.get("role") if isinstance(payload, dict) else None
    return role if isinstance(role, str) else None


def _taken_from_call(recorder: CliffResultRecorder) -> bool:
    """Return whether the cliff result is no longer running under this call.

    A segmented calculation checks it between polls. A failed read is
    logged and treated as not taken, so the calculation goes on.
    """
    try:
        return not recorder.still_held()
    except Exception as error:  # noqa: BLE001  # a failed read must not stop the calculation
        logger.warning(
            "Could not read cliff result %s (%s)",
            recorder.cliff_result_id,
            type(error).__name__,
        )
        return False


def _record_failure(
    error: Exception,
    recorder: CliffResultRecorder,
    *,
    runtime: ObservabilityRuntime | None,
    progress: StageProgress,
) -> None:
    """Report a failure once, then fail the cliff result while this call holds it."""
    if isinstance(error, Stage12ExecutionError):
        # A segmented calculation reports its segment failure where it
        # occurs, before raising this error.
        detail = error.detail
    else:
        detail = report_failure(
            error,
            FailureSite(
                scope="cliff_calculation",
                default_code="cliff_calculation_failed",
                attributes={
                    "simulation_role": progress.process_role,
                    "cliff_result_id": str(recorder.cliff_result_id),
                },
            ),
            runtime=runtime,
            progress=progress,
        )
    record_failure_cause(runtime, error, detail.error_summary)
    persist_failure_record(
        partial(recorder.fail, detail),
        runtime=runtime,
        failure_scope="cliff_calculation",
    )
