"""A detailed-save report run's coordination, on the Public API's run tables alone.

The simulation service creates the report run and its baseline and reform
simulation runs, or names runs other report runs share, starts the
coordinator with the report run's ID, and records the coordinator's call on
the report run. The coordinator claims the report run under its own Modal
call ID, reuses the stored output of each simulation run that already
succeeded, starts a worker for each run pending with no call, naming the run
that worker claims under its own call ID, and waits for each run another call
holds (``shared_simulation_runs``). It then aggregates both outputs and
completes the report run with its result rows. No call creates or claims an
evaluation record.

A report run's simulation runs may be other report runs' too, so the
coordinator never cancels a worker or fails a run for its own report run's
sake. Modal runs a pre-empted coordinator again under the same call ID: the
pre-empted call stops nothing, and the restarted one resumes its claim and
waits for the workers its earlier attempt started. A cancelled coordinator,
which Modal never runs again, fails its report run, and so does a failed one;
both leave their workers to finish. A worker's run is failed only for its own
failure, or once its call ended without an outcome.
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
from .coordinated_simulations import simulation_failure_attributes
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
from .output_planning import resolve_simulation_output_plan, side_read_plan
from .parent_claim import validate_dispatch_context
from .report_run_access import report_run_store
from .report_run_recording import (
    ReportRunRecorder,
    run_error_message,
    run_failure_detail,
)
from .reused_outputs import (
    CloudStorageOutputFiles,
    checked_output,
    descriptor_for_role,
    for_role,
    require_shared_population,
    reused_outputs,
    simulations_to_calculate,
)
from .run_sweep import modal_call_state
from .shared_simulation_runs import (
    ProbedWorkerCall,
    WaitedRun,
    WaitedRunFailedError,
    WaitedRunSucceeded,
    WaitedRunUnstarted,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from uuid import UUID

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.stage12_execution import (
        PlannedSimulationExecutionInput,
        ReportRunIds,
        SimulationExecutionInput,
        Stage12InvocationContext,
        Stage12OutputPlan,
    )
    from policyengine_stage12_persistence import SimulationRunState

    from .coordination_core import CoordinatorDependencies, CoordinatorInvocation
    from .report_run_access import ReportRunWriter
    from .reused_outputs import OutputFileReader
    from .run_sweep import CallState

logger = logging.getLogger(__name__)

# A detailed-save call's report input or context names other runs than the
# report run it claimed.
REPORT_RUN_INPUT_MISMATCH = "report_run_input_mismatch"
# A simulation run the report run must calculate has already failed, and
# recorded no failure of its own to repeat.
SIMULATION_RUN_FAILED = "simulation_run_failed"
# A worker returned its output, but its simulation run doesn't record it.
SIMULATION_RUN_NOT_RECORDED = "simulation_run_not_recorded"
# The report asks for cliff impacts, which no shared simulation run stores;
# cliffs come back later as a calculation of each simulation run.
CLIFF_IMPACTS_UNAVAILABLE = "cliff_impacts_unavailable"
CANCELLED_DETAIL = Stage12FailureDetail(
    error_code="report_cancelled",
    error_summary="Modal cancelled the report's coordinator call.",
)
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
    fails the report run with its reason, leaving its simulation runs to their
    workers, and raises a ``RuntimeError`` that carries no calculation detail.
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
        # call ID: record nothing and stop no worker, since other report runs
        # may wait for their runs; the restarted call waits for them.
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
    # The report's own plan: what the aggregation reads from both sides.
    read_plan: Stage12OutputPlan | None = None

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
        """Reuse or calculate both simulations, aggregate them, record the result.

        A report asking for cliff impacts is refused first
        (``cliff_impacts_unavailable``), before any run is read or started.
        """
        if self.report.baseline.options.get("include_cliffs") is True:
            raise Stage12InputError(
                CLIFF_IMPACTS_UNAVAILABLE,
                "Report runs don't calculate cliff impacts yet: shared "
                "simulation runs don't store the cliff columns. Ask with "
                "include_cliffs false.",
            )
        with self.stage_scope(Stage.STAGE12_COORDINATOR_PREPARATION):
            output_plan, planned_simulations = self.plan_simulations()
            self.read_plan = output_plan
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
                read_plan=output_plan,
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
            if self._calculated_as_the_baseline(simulation):
                continue
            self._start_or_wait(simulation)
        # Every worker has been started, and every run another call holds is
        # waited for, before the coordinator observes them together through
        # nonblocking result probes.
        wait_for_children(self, self.calls, self.descriptors)
        self._share_the_baseline_output()
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

    def child_output_plan(
        self,
        simulation: SimulationExecutionInput,
        output_plan: Stage12OutputPlan,  # noqa: ARG002  # the report's plan, which a stored plan never depends on
    ) -> Stage12OutputPlan:
        """Return ``simulation``'s stored plan, whatever report names its run.

        Identical requests share simulation runs, so a run's file holds what
        any report reads from that side; this report reads ``output_plan``.
        """
        resolver = self.dependencies.stored_plan_resolver
        if resolver is not None:
            return resolver(simulation)
        return resolve_simulation_output_plan(simulation, stage_scope=self.stage_scope)

    def _calculated_as_the_baseline(
        self, simulation: PlannedSimulationExecutionInput
    ) -> bool:
        """Whether ``simulation`` is a reform whose run is the baseline's run.

        A report run naming one simulation run on both sides starts one
        worker for it, the baseline's (``_share_the_baseline_output``).
        """
        return (
            simulation.role is SimulationRole.REFORM
            and self.run_ids.names_one_simulation_run
        )

    def _share_the_baseline_output(self) -> None:
        """Give the reform the baseline's output when both name one run."""
        if not self.run_ids.names_one_simulation_run:
            return
        reform = SimulationRole.REFORM
        if "baseline" in self.reused:
            self.reused[reform.value] = for_role(self.reused["baseline"], reform)
        self.descriptors[reform.value] = descriptor_for_role(
            self.descriptors["baseline"], reform
        )

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

        It is the failure the run recorded, as when its worker fails during
        the report, whole, so its correlation ID stays; a run that recorded
        none gives ``simulation_run_failed``.
        """
        if state.error_message:
            return run_failure_detail(state.error_message)
        return failure_detail_from_record(
            error_code=SIMULATION_RUN_FAILED,
            error_summary=(
                f"The {role} simulation run {self.simulation_run_id(role)} "
                "already failed."
            ),
        )

    def _start_or_wait(self, simulation: PlannedSimulationExecutionInput) -> None:
        """Start the run's worker, or wait for the call that already holds it.

        A run pending with no call is started here, whichever report run it
        was created with; the worker's claim lets one call hold it, and a
        worker that finds it held returns ``deduplicated``. A run pending or
        running under a call is another report run's worker's, or an earlier
        attempt's, and is waited for, never cancelled or released
        (``shared_simulation_runs``).
        """
        role = simulation.role.value
        holder_id = self.run_states[role].modal_call_id
        if holder_id is None:
            self._start_worker(simulation)
        else:
            self.calls[role] = self._waited_run(role, holder_id)

    def _waited_run(self, role: str, holder_id: str | None) -> WaitedRun:
        """Return the wait for ``role``'s run, held by ``holder_id``."""
        return WaitedRun(
            simulation_run_id=self.simulation_run_id(role),
            writer=self.writer,
            restore=self.child_invoker.restore,
            call_state=self._call_state(),
            object_id=holder_id or "",
        )

    def _call_state(self) -> Callable[[str], CallState]:
        """Return how Modal's state of a call is read, by default the sweep's way."""
        return self.dependencies.call_state or modal_call_state

    def _start_worker(self, simulation: PlannedSimulationExecutionInput) -> None:
        """Start the worker that claims the simulation's run.

        A failed start fails the report run (``Stage12ExecutionError``) and
        leaves the run pending with no call, for another report run naming
        it to start, and the workers already started to finish.
        """
        role = simulation.role.value
        child_context = self.context.model_copy(
            update={
                "report_run": None,
                "simulation_run_id": self.simulation_run_id(role),
            }
        )
        try:
            call = self.spawn_child(simulation, child_context.model_dump(mode="json"))
        except Exception as error:  # noqa: BLE001  # any start failure fails the report run
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
            raise Stage12ExecutionError(detail) from None
        self.calls[role] = ProbedWorkerCall(call, self._call_state())

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
        """Return a finished child's descriptor, or None to wait on another call.

        A waited-for run that succeeded is read from its stored file and
        checked, as a reused run is. One left pending with no call gets this
        coordinator's own worker. A worker of this coordinator that found its
        run held by another call returns a ``deduplicated`` summary; the
        coordinator then waits for that run instead.
        """
        if isinstance(result, WaitedRunSucceeded):
            simulation = self.simulations[role]
            other_role = "reform" if role == "baseline" else "baseline"
            require_shared_population(
                result.state,
                self.run_states[other_role],
                named=f"The waited-for {role} simulation run "
                f"{self.simulation_run_id(role)}",
                other_named=f"{other_role} simulation run "
                f"{self.simulation_run_id(other_role)}",
            )
            with self.span(Stage.STAGE12_ARTIFACT_READ, {"simulation_role": role}):
                reused = checked_output(
                    simulation,
                    self.simulation_run_id(role),
                    result.state,
                    files=self._output_files(),
                    required_plan=(
                        None
                        if self.read_plan is None
                        else side_read_plan(self.read_plan, simulation.output_plan)
                    ),
                )
            self.reused[role] = reused
            return reused.descriptor
        if isinstance(result, WaitedRunUnstarted):
            self._start_worker(self.simulations[role])
            return None
        if isinstance(result, dict) and result.get("deduplicated"):
            logger.warning(
                "The %s simulation run of report run %s is held by another call; "
                "waiting for it",
                role,
                self.run_ids.report_run_id,
            )
            self.calls[role] = self._waited_run(role, None)
            return None
        return SimulationArtifactDescriptor.model_validate(result)

    def child_failure(self, role: str, error: Exception) -> Stage12FailureDetail:
        """Report a failed child call; return the failure its report records.

        A waited-for run's failure is the one the run recorded, and nothing
        is written. For this coordinator's own worker, the failure the worker
        recorded on its run takes precedence over the error its call raised;
        a run the worker still holds is failed while it does. A failed read
        or write of the run is logged, never raised, so it never replaces the
        worker's failure.
        """
        if isinstance(error, WaitedRunFailedError):
            return error.detail
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
        call = self.calls[role]
        if isinstance(call, WaitedRun):
            return detail
        try:
            state = self.writer.get_simulation_run(self.simulation_run_id(role))
        except Exception as state_error:  # noqa: BLE001  # the call's own failure is used when the run can't be read
            self._log_run_write_failure(role, state_error)
        else:
            if state.status is RunStatus.FAILED and state.error_message:
                return run_failure_detail(state.error_message)
        self._fail_run(role, detail, modal_call_id=call.object_id)
        return detail

    def cancel_children(self, roles: set[str], detail: Stage12FailureDetail) -> None:
        """Leave every started worker to finish, and every run as it is.

        A report run's simulation runs may be other report runs' too, so a
        report run that fails for one side, for its wait window or for
        anything else stops none of them: a worker finishes its run, whose
        output stays reusable, and a waited-for run is its holder's.
        """

    def _fail_run(
        self, role: str, detail: Stage12FailureDetail, *, modal_call_id: str
    ) -> None:
        """Fail ``role``'s run while ``modal_call_id`` holds it running."""
        try:
            self.writer.fail_simulation_run(
                self.simulation_run_id(role),
                error_message=run_error_message(
                    detail.error_code, detail.error_summary
                ),
                failed_at=datetime.now(UTC),
                modal_call_id=modal_call_id,
            )
        except Exception as error:  # noqa: BLE001  # a failed write is logged and must not replace the failure that caused it
            self._log_run_write_failure(role, error)

    def _log_run_write_failure(self, role: str, error: Exception) -> None:
        logger.error(
            "Could not read or record the %s simulation run %s of report run %s (%s)",
            role,
            self.simulation_run_id(role),
            self.run_ids.report_run_id,
            type(error).__name__,
        )

    def fail(self, error: Exception) -> None:
        """Record a failed report run once, leaving its simulation runs as they are.

        A ``Stage12ExecutionError`` arrives already reported: a worker's,
        start's or wait window's failure, or a run found failed before any
        started. Any other failure is reported here. Only the report run is
        written: its workers finish, and other report runs may name its
        simulation runs.
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
        record_failure_cause(self.runtime, error, detail.error_summary)
        persist_failure_record(
            partial(self.report_run.fail, detail),
            runtime=self.runtime,
            failure_scope="report_coordination",
        )

    def record_cancellation(self) -> None:
        """Fail the report run of a cancelled call, leaving its workers to finish."""
        self.report_run.fail_quietly(CANCELLED_DETAIL, runtime=self.runtime)
