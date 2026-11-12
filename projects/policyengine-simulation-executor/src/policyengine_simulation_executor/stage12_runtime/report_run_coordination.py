"""A detailed-save report run's coordination, on the Public API's run tables alone.

The simulation service creates the report run and its baseline and reform
simulation runs, starts the coordinator with the report run's ID, and
records the coordinator's call on the report run. The coordinator claims the
report run under its own Modal call ID, reuses the stored output of each
simulation run that already succeeded, and starts a worker for each other
one, naming the simulation run that worker claims under its own call ID. It
then aggregates both outputs and completes the report run with its result
rows. No call creates or claims an evaluation record.

Modal runs a pre-empted coordinator again under the same call ID: the
restarted call resumes its claim, stops each worker its earlier attempt
started that holds a simulation run, releases that run and starts a new
worker on it. One that finished meanwhile is used as it finished, and one
an uncancelled earlier worker claims later is waited for. A cancelled
coordinator, which Modal never runs again, fails the report run and the
simulation runs it left unfinished; so does a failed one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactPayload,
    ReportExecutionInput,
    SimulationArtifactDescriptor,
    SimulationRole,
)
from policyengine_simulation_observability.stages import (
    STAGE12_CANONICAL_REPORT_STAGES,
    Stage,
)
from policyengine_stage12_persistence import RunStatus

from .child_wait import wait_for_children
from .coordinated_simulations import cancellation, simulation_failure_attributes
from .coordination_core import CoordinationCore
from .dependencies import ModalChildInvoker, artifact_store
from .failure_causes import (
    StageProgress,
    StageTracking,
    record_failure_cause,
    tracked_stage,
)
from .failure_recording import FailureSite, persist_failure_record, report_failure
from .failures import (
    Stage12ExecutionError,
    Stage12FailureDetail,
    Stage12InputError,
    failure_detail_from_record,
)
from .parent_claim import validate_dispatch_context
from .partition import should_segment_simulation
from .report_run_access import report_run_store
from .report_run_recording import (
    ReportRunRecorder,
    run_error_message,
    run_failure_detail,
)
from .reused_outputs import (
    CloudStorageOutputFiles,
    checked_output,
    reused_outputs,
    simulations_to_calculate,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.stage12_execution import (
        PlannedSimulationExecutionInput,
        ReportRunIds,
        Stage12InvocationContext,
    )
    from policyengine_stage12_persistence import SimulationRunState

    from .coordination_core import CoordinatorDependencies, CoordinatorInvocation
    from .report_run_access import ReportRunWriter
    from .reused_outputs import OutputFileReader

logger = logging.getLogger(__name__)

# A detailed-save call's report input or context names other runs than the
# report run it claimed.
REPORT_RUN_INPUT_MISMATCH = "report_run_input_mismatch"
# A simulation run the report run must calculate has already failed.
SIMULATION_RUN_FAILED = "simulation_run_failed"
# A simulation run changed while the coordinator restarted it.
SIMULATION_RUN_CHANGED = "simulation_run_changed"
# A worker returned its output, but its simulation run doesn't record it.
SIMULATION_RUN_NOT_RECORDED = "simulation_run_not_recorded"
CANCELLED_DETAIL = Stage12FailureDetail(
    error_code="report_cancelled",
    error_summary="Modal cancelled the report's coordinator call.",
)
REPORT_FAILED = cancellation("the report failed")
COORDINATOR_CANCELLED = cancellation("the coordinator's call was cancelled")
ROLES = (SimulationRole.BASELINE, SimulationRole.REFORM)


def coordinate_report_run(
    payload: object,
    context: Stage12InvocationContext,
    *,
    invocation: CoordinatorInvocation,
    dependencies: CoordinatorDependencies,
) -> dict[str, Any]:
    """Claim the report run ``context`` names, coordinate it, and record its result.

    Returns ``{"report_run_id", "status"}``, or the same marked
    ``deduplicated`` without running when another call holds the run or it
    has finished. The run stores and buckets are opened first, so a missing
    setting raises before the claim. Once this call holds the run, any error
    fails the run with its reason, its unfinished simulation runs with it,
    and raises a ``RuntimeError`` that carries no calculation detail.
    """
    # Open the shared child-wait window before any setup work.
    child_wait_deadline = dependencies.monotonic() + dependencies.wait_timeout_seconds
    run_ids = context.report_run
    if run_ids is None:
        msg = "a report run's call names its report run"
        raise ValueError(msg)
    runtime = dependencies.runtime
    report_run = ReportRunRecorder(
        writer=dependencies.report_runs or report_run_store(runtime),
        run_ids=run_ids,
        modal_call_id=invocation.invocation_id,
    )
    artifacts = dependencies.artifacts or artifact_store(report_run=True)
    progress = StageProgress()
    tracking = StageTracking(STAGE12_CANONICAL_REPORT_STAGES, progress, runtime)
    summary = _claim(report_run, tracking=tracking)
    if summary is not None:
        return summary
    coordination: ReportRunCoordination | None = None
    try:
        report = _checked_report(
            payload, context, run_ids, invocation=invocation, writer=report_run.writer
        )
        coordination = ReportRunCoordination(
            report=report,
            context=context,
            invocation=invocation,
            artifact_storage=artifacts,
            child_invoker=dependencies.invoker or ModalChildInvoker(),
            dependencies=dependencies,
            stage_plan=STAGE12_CANONICAL_REPORT_STAGES,
            progress=progress,
            child_wait_deadline=child_wait_deadline,
            report_run=report_run,
        )
        return coordination.run()
    except KeyboardInterrupt:
        # Modal pre-empts a call with SIGINT and runs it again under the same
        # call ID: stop the started workers and record nothing, so the
        # restarted call starts them again.
        if coordination is not None:
            coordination.stop_children()
        raise
    except Exception as error:  # noqa: BLE001  # every failure is recorded before a stable exception returns to Modal
        _fail_unless_cancelled(
            report_run, coordination, error, progress=progress, runtime=runtime
        )
        msg = "Stage 12 report coordination failed"
        raise RuntimeError(msg) from None
    except BaseException:
        # Modal cancels a call with InputCancellation, which is not an
        # Exception, and never runs it again.
        if coordination is None:
            report_run.fail_quietly(CANCELLED_DETAIL, runtime=runtime)
        else:
            coordination.record_cancellation()
        raise


def _claim(
    report_run: ReportRunRecorder, *, tracking: StageTracking
) -> dict[str, Any] | None:
    """Claim the report run; return the summary to return instead, if any.

    The claim may commit before an error, or Modal's cancellation, stops it:
    the run is then failed, which changes nothing if this call doesn't hold
    it. A pre-emption propagates, since Modal runs the call again.
    """
    try:
        with tracked_stage(Stage.STAGE12_COORDINATOR_CLAIM, None, tracking=tracking):
            claimed = report_run.claim()
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        report_run.fail_quietly(_claim_failure(error), runtime=tracking.runtime)
        raise
    if claimed.changed:
        return None
    return {
        "deduplicated": True,
        "report_run_id": str(report_run.run_ids.report_run_id),
        "status": claimed.status.value,
    }


def _claim_failure(error: BaseException) -> Stage12FailureDetail:
    """Describe an error, or Modal's cancellation, that stopped the claim."""
    if not isinstance(error, Exception):
        return CANCELLED_DETAIL
    return Stage12FailureDetail(
        error_code="report_claim_failed",
        error_summary=f"Claiming the report run failed ({type(error).__name__}).",
    )


def _checked_report(
    payload: object,
    context: Stage12InvocationContext,
    run_ids: ReportRunIds,
    *,
    invocation: CoordinatorInvocation,
    writer: ReportRunWriter,
) -> ReportExecutionInput:
    """Return the report input, checked against the report run the call claimed.

    The input's evaluation ID is the report run's, each simulation's
    execution ID is its simulation run's, the artifact prefix names the
    report run, and the report run row names those simulation runs. Raises
    ``Stage12InputError`` when they differ.
    """
    report = ReportExecutionInput.model_validate(payload)
    try:
        validate_dispatch_context(report, context, invocation.application_name)
    except ValueError as error:
        raise Stage12InputError(REPORT_RUN_INPUT_MISMATCH, str(error)) from error
    named = {
        "evaluation_id": (report.evaluation_id, run_ids.report_run_id),
        "baseline simulation_execution_id": (
            report.baseline.simulation_execution_id,
            run_ids.baseline_simulation_run_id,
        ),
        "reform simulation_execution_id": (
            report.reform.simulation_execution_id,
            run_ids.reform_simulation_run_id,
        ),
    }
    for name, (given, expected) in named.items():
        if given != expected:
            raise Stage12InputError(
                REPORT_RUN_INPUT_MISMATCH,
                f"The report input's {name} is {given}, not its run's {expected}.",
            )
    if str(run_ids.report_run_id) not in context.artifact_prefix:
        raise Stage12InputError(
            REPORT_RUN_INPUT_MISMATCH,
            "The call's artifact prefix does not name its report run.",
        )
    try:
        writer.require_simulation_runs(run_ids)
    except ValueError as error:
        raise Stage12InputError(REPORT_RUN_INPUT_MISMATCH, str(error)) from error
    return report


def _fail_unless_cancelled(
    report_run: ReportRunRecorder,
    coordination: ReportRunCoordination | None,
    error: Exception,
    *,
    progress: StageProgress,
    runtime: ObservabilityRuntime | None,
) -> None:
    """Record ``error``; if Modal cancels the call meanwhile, still fail the run.

    Before the coordination exists, only the report run is failed: the
    call's input wasn't checked against its simulation runs yet.
    """
    try:
        if coordination is not None:
            coordination.fail(error)
            return
        detail = report_failure(
            error,
            FailureSite(
                scope="report_coordination",
                default_code="report_coordination_failed",
                attributes={"report_run_id": str(report_run.run_ids.report_run_id)},
            ),
            runtime=runtime,
            progress=progress,
        )
        record_failure_cause(runtime, error, detail.error_summary)
        report_run.fail_quietly(detail, runtime=runtime)
    except KeyboardInterrupt:
        raise
    except BaseException:
        report_run.fail_quietly(CANCELLED_DETAIL, runtime=runtime)
        raise


@dataclass(kw_only=True)
class ReportRunCoordination(CoordinationCore):
    """One claimed report run, its simulation runs' states, and their workers."""

    report_run: ReportRunRecorder
    # Each simulation run's state by role, as read while preparing.
    run_states: dict[str, SimulationRunState] = field(default_factory=dict)

    @property
    def writer(self) -> ReportRunWriter:
        """The run tables' writer, as the Modal worker role."""
        return self.report_run.writer

    @property
    def run_ids(self) -> ReportRunIds:
        """The report run and its two simulation runs."""
        return self.report_run.run_ids

    def simulation_run_id(self, role: str) -> UUID:
        """Return the simulation run of ``role``."""
        return self.run_ids.simulation_run_id(SimulationRole(role))

    def run(self) -> dict[str, Any]:
        """Reuse or calculate both simulations, aggregate them, record the result."""
        with self.stage_scope(Stage.STAGE12_COORDINATOR_PREPARATION):
            output_plan, planned_simulations = self.plan_simulations()
            self.run_states = {
                role.value: self.writer.get_simulation_run(
                    self.run_ids.simulation_run_id(role)
                )
                for role in ROLES
            }
            self.reused = reused_outputs(
                planned_simulations,
                run_ids=self.run_ids,
                stored_runs={role: self.run_states[role.value] for role in ROLES},
                files=self.dependencies.output_files,
                span=self.span,
            )
        self.simulations = {
            simulation.role.value: simulation for simulation in planned_simulations
        }
        self.descriptors.update(
            {role: output.descriptor for role, output in self.reused.items()}
        )
        calculated = simulations_to_calculate(planned_simulations, self.reused)
        self._require_unfailed(calculated)
        for simulation in calculated:
            self._start_worker(simulation)
        # Every worker has been started before the coordinator observes them
        # together through nonblocking result probes.
        wait_for_children(self, self.calls, self.descriptors)
        self._require_recorded(calculated)
        aggregate, _baseline, _reform = self.aggregate(output_plan, self.descriptors)
        result = AggregateReportArtifactPayload.model_validate(aggregate).result
        written = self.report_run.complete(
            result, country=self.report.baseline.geography.country
        )
        if not written.changed:
            logger.warning(
                "Report run %s finished, but this call no longer holds it: it is %s",
                self.run_ids.report_run_id,
                written.status.value,
            )
        return {
            "report_run_id": str(self.run_ids.report_run_id),
            "status": written.status.value,
        }

    def _require_unfailed(
        self, simulations: Sequence[PlannedSimulationExecutionInput]
    ) -> None:
        """Raise before any worker starts if a run to calculate already failed."""
        for simulation in simulations:
            role = simulation.role.value
            state = self.run_states[role]
            if state.status is RunStatus.FAILED:
                raise Stage12ExecutionError(self._already_failed(role, state))

    def _already_failed(
        self, role: str, state: SimulationRunState
    ) -> Stage12FailureDetail:
        """Describe a run this report must calculate that already failed.

        The summary ends with the failure the run recorded.
        """
        recorded = f": {state.error_message}" if state.error_message else ""
        return failure_detail_from_record(
            error_code=SIMULATION_RUN_FAILED,
            error_summary=(
                f"The {role} simulation run {self.simulation_run_id(role)} "
                f"already failed{recorded}"
            ),
        )

    def _start_worker(self, simulation: PlannedSimulationExecutionInput) -> None:
        """Start the worker that claims the simulation's run, unless none is needed.

        A run an earlier attempt's worker holds is released first, after
        that worker is asked to stop; one it finished meanwhile is used as
        stored, and one another call holds is waited for, with no new
        worker (see ``_release_from_earlier_worker``). A failed start fails
        the run, stops every started peer and raises
        ``Stage12ExecutionError``.
        """
        role = simulation.role.value
        run_id = self.simulation_run_id(role)
        earlier_call_id = self.run_states[role].modal_call_id
        if earlier_call_id is not None:
            try:
                needs_worker = self._release_from_earlier_worker(
                    simulation, earlier_call_id
                )
            except Stage12ExecutionError:
                # The workers started before this run are stopped too.
                self.cancel_children(
                    set(self.calls), cancellation(f"the {role} simulation failed")
                )
                raise
            if not needs_worker:
                return
        child_context = self.context.model_copy(
            update={"report_run": None, "simulation_run_id": run_id}
        )
        try:
            call = self.spawn_child(simulation, child_context.model_dump(mode="json"))
        except Exception as error:  # noqa: BLE001  # any start failure fails the run and stops its started peers
            detail = report_failure(
                error,
                FailureSite(
                    scope="child_dispatch",
                    default_code="simulation_dispatch_failed",
                    attributes=simulation_failure_attributes(simulation),
                ),
                runtime=self.runtime,
                progress=self.progress,
            )
            self._fail_run(role, detail)
            self.cancel_children(
                set(self.calls), cancellation(f"the {role} simulation failed")
            )
            raise Stage12ExecutionError(detail) from None
        self.calls[role] = call

    def _release_from_earlier_worker(
        self, simulation: PlannedSimulationExecutionInput, earlier_call_id: str
    ) -> bool:
        """Stop the earlier attempt's worker, release its run; return if one is needed.

        The earlier worker may have finished the run, or another call may hold
        it, before the release: a succeeded run's stored output is used, a
        run another call holds is waited for, and a failed run fails the
        report with its own cause (``Stage12ExecutionError``).
        """
        role = simulation.role.value
        try:
            self.child_invoker.restore(earlier_call_id).cancel()
        except Exception as error:  # noqa: BLE001  # stopping an earlier attempt's worker is best effort; releasing its run refuses its writes
            logger.warning(
                "Could not cancel an earlier %s worker of report run %s (%s)",
                role,
                self.run_ids.report_run_id,
                type(error).__name__,
            )
        run_id = self.simulation_run_id(role)
        released = self.writer.release_simulation_run(
            run_id, modal_call_id=earlier_call_id, released_at=datetime.now(UTC)
        )
        if released.changed:
            return True
        state = self.writer.get_simulation_run(run_id)
        # Later failures leave a run read as succeeded or failed unwritten.
        self.run_states[role] = state
        if state.status is RunStatus.SUCCEEDED:
            with self.span(Stage.STAGE12_ARTIFACT_READ, {"simulation_role": role}):
                reused = checked_output(
                    simulation, run_id, state, files=self._output_files()
                )
            self.reused[role] = reused
            self.descriptors[role] = reused.descriptor
            return False
        if state.status is RunStatus.FAILED:
            raise Stage12ExecutionError(self._already_failed(role, state))
        if state.status is RunStatus.RUNNING and state.modal_call_id is not None:
            self.calls[role] = self.child_invoker.restore(state.modal_call_id)
            return False
        if state.status is RunStatus.PENDING and state.modal_call_id is None:
            return True
        raise Stage12ExecutionError(
            Stage12FailureDetail(
                error_code=SIMULATION_RUN_CHANGED,
                error_summary=(
                    f"The {role} simulation run {run_id} became "
                    f"{state.status.value} while it was restarted."
                ),
            )
        )

    def _output_files(self) -> OutputFileReader:
        """Return the reader of stored output files, by default Cloud Storage's."""
        return self.dependencies.output_files or CloudStorageOutputFiles()

    def _require_recorded(
        self, simulations: Sequence[PlannedSimulationExecutionInput]
    ) -> None:
        """Raise unless each calculated run succeeded with its worker's output."""
        for simulation in simulations:
            role = simulation.role.value
            state = self.writer.get_simulation_run(self.simulation_run_id(role))
            descriptor: SimulationArtifactDescriptor = self.descriptors[role]
            if (
                state.status is not RunStatus.SUCCEEDED
                or state.output_uri != descriptor.artifact.uri
            ):
                raise Stage12ExecutionError(
                    Stage12FailureDetail(
                        error_code=SIMULATION_RUN_NOT_RECORDED,
                        error_summary=(
                            f"The {role} simulation run "
                            f"{self.simulation_run_id(role)} is "
                            f"{state.status.value}, without its worker's output."
                        ),
                    )
                )

    def child_descriptor(
        self, role: str, result: object
    ) -> SimulationArtifactDescriptor | None:
        """Return a finished worker's descriptor, or None to wait on another call.

        A worker that found its run held by another call returns a
        ``deduplicated`` summary: a worker an earlier attempt started, whose
        cancellation didn't reach it, claimed the run first. The coordinator
        then waits for that call, which records the run, instead. Raises
        ``RuntimeError`` for a run that failed or holds no other call.
        """
        if not (isinstance(result, dict) and result.get("deduplicated")):
            return SimulationArtifactDescriptor.model_validate(result)
        state = self.writer.get_simulation_run(self.simulation_run_id(role))
        holder = state.modal_call_id
        if (
            state.status in {RunStatus.RUNNING, RunStatus.SUCCEEDED}
            and holder is not None
            and holder != self.calls[role].object_id
        ):
            logger.warning(
                "The %s simulation run of report run %s is held by an earlier "
                "worker; waiting for it",
                role,
                self.run_ids.report_run_id,
            )
            self.calls[role] = self.child_invoker.restore(holder)
            return None
        msg = f"the {role} worker's run is {state.status.value} without its result"
        raise RuntimeError(msg)

    def child_failure(self, role: str, error: Exception) -> Stage12FailureDetail:
        """Report a failed worker call; return the failure its run records.

        The failure the worker recorded on its run takes precedence over the
        error its call raised. A failed read or write of the run is logged,
        never raised, so it never replaces the worker's failure.
        """
        detail = report_failure(
            error,
            FailureSite(
                scope="child_wait",
                default_code="simulation_invocation_failed",
                attributes=simulation_failure_attributes(self.simulations[role]),
            ),
            runtime=self.runtime,
            progress=self.progress,
        )
        try:
            state = self.writer.get_simulation_run(self.simulation_run_id(role))
        except Exception as state_error:  # noqa: BLE001  # the call's own failure is used when the run can't be read
            self._log_run_write_failure(role, state_error)
        else:
            if state.status is RunStatus.FAILED and state.error_message:
                return run_failure_detail(state.error_message)
        self._fail_run(role, detail)
        return detail

    def cancel_children(self, roles: set[str], detail: Stage12FailureDetail) -> None:
        """Fail the simulation run of each started, unfinished worker, then stop it.

        Only a pending or running run changes, so one that finished keeps its
        outcome. A segmented worker whose run was failed learns of it from
        its run, and cancels the region-group calls it started; Modal does
        not cancel spawned calls, so cancelling the worker would leave them
        running. Write and cancellation errors are logged, never raised.
        """
        for role in sorted(roles):
            recorded = self._fail_run(role, detail)
            if recorded and should_segment_simulation(self.simulations[role]):
                continue
            try:
                self.calls[role].cancel()
            except Exception as error:  # noqa: BLE001  # a failed Modal cancellation is logged and must not replace the failure that caused it
                logger.warning(
                    "Could not cancel the %s worker of report run %s (%s)",
                    role,
                    self.run_ids.report_run_id,
                    type(error).__name__,
                )

    def _fail_run(self, role: str, detail: Stage12FailureDetail) -> bool:
        """Fail ``role``'s run while it is unfinished; return whether the write ran."""
        try:
            self.writer.fail_simulation_run(
                self.simulation_run_id(role),
                error_message=run_error_message(
                    detail.error_code, detail.error_summary
                ),
                failed_at=datetime.now(UTC),
            )
        except Exception as error:  # noqa: BLE001  # a failed write is logged and must not replace the failure that caused it
            self._log_run_write_failure(role, error)
            return False
        return True

    def _log_run_write_failure(self, role: str, error: Exception) -> None:
        logger.error(
            "Could not read or record the %s simulation run %s of report run %s (%s)",
            role,
            self.simulation_run_id(role),
            self.run_ids.report_run_id,
            type(error).__name__,
        )

    def fail_unfinished_runs(self, detail: Stage12FailureDetail) -> None:
        """Fail each simulation run of the report run that is still unfinished.

        A report run's unfinished simulation runs are its coordinator's: the
        simulation service starts no worker on them, and the stuck-run sweep
        leaves them to it while the report run is unfinished. A run last read
        as succeeded or failed is never written: other report runs may reuse
        a succeeded one.
        """
        for role in ROLES:
            state = self.run_states.get(role.value)
            if state is not None and state.status in {
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
            }:
                continue
            self._fail_run(role.value, detail)

    def fail(self, error: Exception) -> None:
        """Record a failed report run once, after stopping its unfinished workers.

        A ``Stage12ExecutionError`` arrives already reported: a worker's,
        start's, release's or wait window's failure, after the unfinished
        workers were stopped, or a run found failed before any started. Any
        other failure is reported here, before any write, and then the
        started, unfinished workers are stopped. The report run's unfinished
        simulation runs then fail, and the report run last.
        """
        if isinstance(error, Stage12ExecutionError):
            detail = error.detail
        else:
            detail = report_failure(
                error,
                FailureSite(
                    scope="report_coordination",
                    default_code="report_coordination_failed",
                    attributes={"report_run_id": str(self.run_ids.report_run_id)},
                ),
                runtime=self.runtime,
                progress=self.progress,
            )
            self.cancel_children(
                set(self.calls) - set(self.descriptors),
                cancellation("the coordinator failed"),
            )
        record_failure_cause(self.runtime, error, detail.error_summary)
        persist_failure_record(
            partial(self.fail_unfinished_runs, REPORT_FAILED),
            runtime=self.runtime,
            failure_scope="report_coordination",
        )
        persist_failure_record(
            partial(self.report_run.fail, detail),
            runtime=self.runtime,
            failure_scope="report_coordination",
        )

    def record_cancellation(self) -> None:
        """Stop and fail the unfinished workers and runs, then fail the report run."""
        self.cancel_children(
            set(self.calls) - set(self.descriptors), COORDINATOR_CANCELLED
        )
        persist_failure_record(
            partial(self.fail_unfinished_runs, COORDINATOR_CANCELLED),
            runtime=self.runtime,
            failure_scope="report_coordination",
        )
        self.report_run.fail_quietly(CANCELLED_DETAIL, runtime=self.runtime)
