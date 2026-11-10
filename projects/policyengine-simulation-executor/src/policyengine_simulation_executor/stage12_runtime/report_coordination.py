"""One claimed report's coordination: start, wait for and aggregate its simulations."""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactDescriptor,
    ComparisonReportRecord,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    PlannedSimulationExecutionInput,
    ReportExecutionInput,
    SimulationArtifactDescriptor,
    SimulationRole,
    Stage12InvocationContext,
    Stage12OutputPlan,
)
from policyengine_simulation_observability.stages import Stage, StagePlan
from policyengine_stage12_persistence import RunStatus

from policyengine_simulation_executor.stage12_artifacts import (
    Stage12ArtifactStore,
    deserialize_calculation_provenance,
    deserialize_simulation_frames,
    deserialize_uk_local_authority_metadata,
)
from policyengine_simulation_executor.stage12_timeouts import (
    SIMULATION_WAIT_TIMEOUT_SECONDS,
)

from .aggregation import (
    RetainedSimulationOutput,
    build_aggregate_report,
    validate_aligned_outputs,
    validate_uk_local_authority_metadata,
)
from .comparison import ComparisonServices, compare_completed_report
from .coordinated_simulations import (
    cancel_pending_simulations,
    cancellation,
    child_failure_detail,
    child_record,
    logger,
    record_child_failure,
    simulation_failure_attributes,
)
from .dependencies import ChildCall, ChildInvoker, ChildSpawnRequest, ComparisonStore
from .failure_causes import (
    StageProgress,
    StageTracking,
    record_failure_cause,
    tracked_stage,
)
from .failure_recording import (
    FailureSite,
    persist_failure_record,
    report_failure,
    report_original_failure,
)
from .failures import Stage12ExecutionError, Stage12FailureDetail
from .output_planning import (
    plan_simulation_input,
    resolve_report_output_plan,
    validate_output_frames,
)
from .report_run_recording import SimulationRunRecordingStore
from .reused_outputs import output_payload, reused_outputs, simulations_to_calculate
from .simulation import descriptor_from_record

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from contextlib import AbstractContextManager

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata

    from .report_run_access import ReportRunWriter
    from .report_run_recording import ReportRunRecorder
    from .reused_outputs import OutputFileReader, ReusedSimulationOutput

SIMULATION_POLL_INITIAL_SECONDS = 0.25
SIMULATION_POLL_MAX_SECONDS = 1.0
SIMULATION_POLL_BACKOFF_FACTOR = 2.0


def _next_poll_interval(current: float) -> float:
    return min(
        current * SIMULATION_POLL_BACKOFF_FACTOR,
        SIMULATION_POLL_MAX_SECONDS,
    )


def _require_artifact_digest(
    payload: bytes,
    expected_sha256: str,
    *,
    mismatch_message: str,
) -> None:
    """Raise ``ValueError(mismatch_message)`` unless ``payload`` matches its digest."""
    if sha256(payload).hexdigest() != expected_sha256:
        raise ValueError(mismatch_message)


@dataclass(frozen=True)
class CoordinatorInvocation:
    """The deployed coordinator call running a report: its Modal app and call ID."""

    application_name: str
    invocation_id: str


@dataclass(frozen=True)
class CoordinatorDependencies:
    """Seams the coordinator uses; an unset seam selects the deployed default.

    The defaults are the Stage 12 database store, the private artifact
    bucket, Modal child invocation, the standard aggregate builder and output
    plan resolution, no telemetry runtime, the real clock and sleep, the
    standard child wait window, and the report run store and the Cloud
    Storage reader of reused output files, which only a call that names a
    report run uses.
    """

    store: ComparisonStore | None = None
    artifacts: Stage12ArtifactStore | None = None
    invoker: ChildInvoker | None = None
    aggregator: Callable[..., dict[str, Any]] = build_aggregate_report
    output_plan_resolver: Callable[[ReportExecutionInput], Stage12OutputPlan] | None = (
        None
    )
    runtime: ObservabilityRuntime | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    wait_timeout_seconds: float = SIMULATION_WAIT_TIMEOUT_SECONDS
    report_runs: ReportRunWriter | None = None
    output_files: OutputFileReader | None = None


@dataclass
class ReportCoordination:
    """One claimed report's inputs and resolved seams, run through each phase."""

    report: ReportExecutionInput
    context: Stage12InvocationContext
    invocation: CoordinatorInvocation
    persistence: ComparisonStore
    artifact_storage: Stage12ArtifactStore
    child_invoker: ChildInvoker
    dependencies: CoordinatorDependencies
    stage_plan: StagePlan
    # The last stage entered and whether it finished, for failure causes.
    progress: StageProgress
    # The shared child-wait window closes at this monotonic time.
    child_wait_deadline: float
    # Each child by role: its plan, its started call and its descriptor once
    # finished, so a failure at any point can stop the unfinished children.
    simulations: dict[str, PlannedSimulationExecutionInput] = dataclasses.field(
        default_factory=dict
    )
    calls: dict[str, ChildCall] = dataclasses.field(default_factory=dict)
    descriptors: dict[str, SimulationArtifactDescriptor] = dataclasses.field(
        default_factory=dict
    )
    # The report run the call names, when it records one.
    report_run: ReportRunRecorder | None = None
    # Each reused simulation run's checked output by role, in place of a child.
    reused: dict[str, ReusedSimulationOutput] = dataclasses.field(default_factory=dict)

    @property
    def runtime(self) -> ObservabilityRuntime | None:
        """The telemetry runtime, or None when the coordinator records nothing."""
        return self.dependencies.runtime

    @property
    def stage_tracking(self) -> StageTracking:
        """How this report's stages are named, recorded and timed."""
        return StageTracking(self.stage_plan, self.progress, self.runtime)

    def span(
        self,
        stage: Stage,
        attributes: Mapping[str, object] | None = None,
    ) -> AbstractContextManager[object]:
        """Track ``stage`` for failure causes and time it as a span."""
        return tracked_stage(stage, attributes, tracking=self.stage_tracking)

    def stage_scope(
        self,
        stage: Stage,
        attributes: Mapping[str, object] | None = None,
    ) -> AbstractContextManager[object]:
        """Track ``stage`` for failure causes and record it as an operation."""
        return tracked_stage(
            stage, attributes, tracking=self.stage_tracking, operation=True
        )

    def run(self, parent: ComparisonReportRecord) -> dict[str, Any]:
        """Plan, reuse or start and await both outputs, then aggregate them."""
        output_plan, planned_simulations = self._prepare(parent)
        self.simulations = {
            simulation.role.value: simulation for simulation in planned_simulations
        }
        self.descriptors.update(
            {role: output.descriptor for role, output in self.reused.items()}
        )
        calculated = simulations_to_calculate(planned_simulations, self.reused)
        children = self._create_children(calculated)
        descriptors, calls = self._start_children(calculated, children)
        # Every required call has been started before the coordinator observes
        # baseline and reform together through nonblocking result probes.
        self._wait_for_children(calls, descriptors)
        return self._aggregate_and_complete(parent, output_plan, descriptors)

    def _prepare(
        self, parent: ComparisonReportRecord
    ) -> tuple[Stage12OutputPlan, tuple[PlannedSimulationExecutionInput, ...]]:
        """Bind the run's artifact prefix, then plan the outputs and child inputs."""
        with self.stage_scope(Stage.STAGE12_COORDINATOR_PREPARATION):
            self.context = self.context.model_copy(
                update={
                    "artifact_prefix": (
                        f"stage-12-runs/{parent.environment}/"
                        f"{parent.created_at:%Y}/{parent.created_at:%m}/"
                        f"{parent.evaluation_id}"
                    ),
                    "created_at": parent.created_at,
                    "retention_expires_at": parent.retention_expires_at,
                }
            )
            with self.stage_scope(
                Stage.STAGE12_OUTPUT_PLANNING,
                {"country": self.report.baseline.geography.country},
            ):
                resolver = self.dependencies.output_plan_resolver
                if resolver is None:
                    output_plan = resolve_report_output_plan(
                        self.report,
                        stage_scope=self.stage_scope,
                    )
                else:
                    output_plan = resolver(self.report)
            simulations = []
            for simulation in (self.report.baseline, self.report.reform):
                with self.stage_scope(
                    Stage.STAGE12_CHILD_INPUT_PLANNING,
                    {"simulation_role": simulation.role.value},
                ):
                    simulations.append(plan_simulation_input(simulation, output_plan))
            self.reused = reused_outputs(self, simulations)
        return output_plan, tuple(simulations)

    def _create_children(
        self, planned_simulations: tuple[PlannedSimulationExecutionInput, ...]
    ) -> dict[SimulationRole, ComparisonSimulationRecord]:
        """Create each child's row, or resolve the row an earlier attempt made."""
        with self.span(Stage.STAGE12_CHILD_STATE_CREATE):
            return {
                simulation.role: self.persistence.create_or_resolve_simulation(
                    child_record(
                        simulation=simulation,
                        context=self.context,
                        function_name=self.context.simulation_callable,
                    )
                ).record
                for simulation in planned_simulations
            }

    def _start_children(
        self,
        planned_simulations: tuple[PlannedSimulationExecutionInput, ...],
        children: dict[SimulationRole, ComparisonSimulationRecord],
    ) -> tuple[dict[str, SimulationArtifactDescriptor], dict[str, ChildCall]]:
        """Reuse finished children, reattach running ones and dispatch the rest."""
        descriptors = self.descriptors
        calls = self.calls
        for simulation in planned_simulations:
            child = children[simulation.role]
            role = simulation.role.value
            if child.status is ComparisonRunLifecycleStatus.SUCCEEDED:
                descriptors[role] = self._retained_descriptor(child, simulation)
            elif (
                child.status is ComparisonRunLifecycleStatus.RUNNING
                and child.modal_invocation_id is not None
                and not child.modal_invocation_id.startswith("dispatch-pending-")
            ):
                if self.report_run is None:
                    calls[role] = self.child_invoker.restore(child.modal_invocation_id)
                else:
                    # A report run's coordinator cancelled this child when it
                    # was pre-empted; make sure it stopped, then start it again.
                    self._cancel_earlier_child(child)
                    calls[role] = self._dispatch_child(child, simulation)
            else:
                calls[role] = self._dispatch_child(child, simulation)
        return descriptors, calls

    def _retained_descriptor(
        self,
        child: ComparisonSimulationRecord,
        simulation: PlannedSimulationExecutionInput,
    ) -> SimulationArtifactDescriptor:
        """Describe a succeeded child's digest-checked retained artifact."""
        descriptor = descriptor_from_record(child, simulation)
        retained = self.artifact_storage.read(descriptor.artifact.uri)
        _require_artifact_digest(
            retained,
            descriptor.artifact.content_sha256,
            mismatch_message="retained child artifact digest mismatch",
        )
        return descriptor.model_copy(
            update={
                "artifact": descriptor.artifact.model_copy(
                    update={"size_bytes": len(retained)}
                ),
                "calculation_provenance": (
                    deserialize_calculation_provenance(retained)
                ),
            }
        )

    def _cancel_earlier_child(self, child: ComparisonSimulationRecord) -> None:
        """Cancel the child call an earlier attempt of this call started."""
        if child.modal_invocation_id is None:
            return
        try:
            self.child_invoker.restore(child.modal_invocation_id).cancel()
        except Exception as error:  # noqa: BLE001  # cancelling an earlier attempt's child is best effort; the new child starts regardless
            logger.warning(
                "Stage 12 could not cancel an earlier %s simulation call in report "
                "%s (%s)",
                child.role.value,
                child.evaluation_id,
                type(error).__name__,
            )

    def stop_children(self) -> None:
        """Cancel every started, unfinished child call, recording nothing.

        Each child's record stays running, so the call Modal restarts starts
        it again. A cancellation error is logged, never raised.
        """
        for role in sorted(set(self.calls) - set(self.descriptors)):
            try:
                self.calls[role].cancel()
            except Exception as error:  # noqa: BLE001  # every child must be asked to stop; a failed cancellation is logged
                logger.warning(
                    "Stage 12 could not cancel the %s simulation of report %s (%s)",
                    role,
                    self.report.evaluation_id,
                    type(error).__name__,
                )

    def _dispatch_child(
        self,
        child: ComparisonSimulationRecord,
        simulation: PlannedSimulationExecutionInput,
    ) -> ChildCall:
        """Mark the child dispatching, spawn it, and attach its Modal call ID.

        A spawn failure is reported and recorded on the child (unless it already
        finished), every started peer is stopped, and Stage12ExecutionError is
        raised with the failure.
        """
        dispatching_at = datetime.now(UTC)
        dispatch_placeholder = f"dispatch-pending-{uuid4()}"
        self.persistence.replace_simulation(
            child.model_copy(
                update={
                    "status": ComparisonRunLifecycleStatus.RUNNING,
                    "modal_invocation_id": dispatch_placeholder,
                    "started_at": child.started_at or dispatching_at,
                    "updated_at": dispatching_at,
                    "error_code": None,
                    "error_summary": None,
                    "completed_at": None,
                }
            )
        )
        try:
            with self.span(
                Stage.STAGE12_CHILD_DISPATCH,
                {"simulation_role": simulation.role.value},
            ):
                child_observability_context = (
                    self.runtime.capture_context() if self.runtime is not None else None
                )
                call = self.child_invoker.spawn(
                    ChildSpawnRequest(
                        application_name=self.invocation.application_name,
                        function_name=self.context.simulation_callable,
                        environment=self.context.modal_environment,
                        simulation=simulation.model_dump(mode="json"),
                        # A comparison run's children receive the context
                        # fields they received before report runs existed.
                        context=self.context.model_dump(
                            mode="json",
                            exclude=(
                                {"report_run"}
                                if self.context.report_run is None
                                else None
                            ),
                        ),
                        observability_context=child_observability_context,
                    )
                )
        except Exception as error:  # noqa: BLE001  # any dispatch failure is recorded on the child and stops its started peers
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
            record_child_failure(
                store=self.persistence,
                simulation=simulation,
                detail=detail,
            )
            cancel_pending_simulations(
                calls=self.calls,
                simulations=self.simulations,
                roles=set(self.calls),
                store=self.persistence,
                detail=cancellation(f"the {simulation.role.value} simulation failed"),
            )
            raise Stage12ExecutionError(detail) from None
        # Record the call before attaching its ID, so a failed attach still
        # stops it.
        self.calls[simulation.role.value] = call
        self.persistence.attach_simulation_invocation(
            simulation.simulation_execution_id,
            expected_placeholder=dispatch_placeholder,
            modal_invocation_id=call.object_id,
            updated_at=datetime.now(UTC),
        )
        return call

    def _wait_for_children(
        self,
        calls: dict[str, ChildCall],
        descriptors: dict[str, SimulationArtifactDescriptor],
    ) -> None:
        """Observe every started child together until each has a descriptor.

        A child's failure, or the shared wait window closing, stops the
        unfinished children and raises Stage12ExecutionError.
        """
        with self.span(Stage.STAGE12_CHILD_WAIT):
            self._observe_children(calls, descriptors)

    def _observe_children(
        self,
        calls: dict[str, ChildCall],
        descriptors: dict[str, SimulationArtifactDescriptor],
    ) -> None:
        """Probe every unfinished child, without blocking, until each finishes.

        Between passes the coordinator sleeps, backing off while no child
        finishes, and never past the shared wait window.
        """
        wait_stage_name = self.stage_plan.name(Stage.STAGE12_CHILD_WAIT)
        timeout_seconds = self.dependencies.wait_timeout_seconds
        pending = set(calls)
        poll_interval = SIMULATION_POLL_INITIAL_SECONDS

        while pending:
            progress_made = False
            for role in sorted(pending):
                call = calls[role]
                try:
                    # The probe records the child's role, so a failure names it.
                    with self.progress.stage(wait_stage_name, role=role):
                        result = call.get(timeout=0)
                        descriptor = SimulationArtifactDescriptor.model_validate(result)
                except TimeoutError:
                    continue
                except Exception as error:  # noqa: BLE001  # a child call's failure can be any exception type; it is recorded and stops the other child
                    detail = child_failure_detail(
                        error=error,
                        simulation=self.simulations[role],
                        store=self.persistence,
                        runtime=self.runtime,
                        progress=self.progress,
                    )
                    cancel_pending_simulations(
                        calls=calls,
                        simulations=self.simulations,
                        roles=pending - {role},
                        store=self.persistence,
                        detail=cancellation(f"the {role} simulation failed"),
                    )
                    raise Stage12ExecutionError(detail) from None
                descriptors[role] = descriptor
                pending.remove(role)
                progress_made = True

            if not pending:
                return
            remaining_seconds = self.child_wait_deadline - self.dependencies.monotonic()
            if remaining_seconds <= 0:
                detail = Stage12FailureDetail(
                    error_code="simulation_wait_timeout",
                    error_summary=(
                        "Stage 12 simulations did not finish within "
                        f"{timeout_seconds:g} seconds of the report coordinator "
                        "starting."
                    ),
                )
                wait_timeout = Stage12ExecutionError(detail)
                report_original_failure(
                    wait_timeout,
                    runtime=self.runtime,
                    failure_scope="child_wait_timeout",
                )
                cancel_pending_simulations(
                    calls=calls,
                    simulations=self.simulations,
                    roles=pending,
                    store=self.persistence,
                    detail=cancellation(
                        f"the {timeout_seconds:g}-second wait window closed"
                    ),
                )
                raise wait_timeout
            # Never sleep past the deadline, so the window closes on time.
            self.dependencies.sleep(min(poll_interval, remaining_seconds))
            poll_interval = (
                SIMULATION_POLL_INITIAL_SECONDS
                if progress_made
                else _next_poll_interval(poll_interval)
            )

    def _read_retained_outputs(
        self,
        output_plan: Stage12OutputPlan,
        baseline: SimulationArtifactDescriptor,
        reform: SimulationArtifactDescriptor,
    ) -> tuple[
        RetainedSimulationOutput,
        RetainedSimulationOutput,
        UKLocalAuthorityMetadata | None,
    ]:
        """Read, digest-check and validate both children's retained artifacts."""
        validate_aligned_outputs(self.report, output_plan, baseline, reform)
        with self.span(Stage.STAGE12_ARTIFACT_READ):
            baseline_payload = output_payload(
                baseline, self.reused, self.artifact_storage
            )
            reform_payload = output_payload(reform, self.reused, self.artifact_storage)
        _require_artifact_digest(
            baseline_payload,
            baseline.artifact.content_sha256,
            mismatch_message="baseline artifact digest mismatch",
        )
        _require_artifact_digest(
            reform_payload,
            reform.artifact.content_sha256,
            mismatch_message="reform artifact digest mismatch",
        )
        baseline_frames = deserialize_simulation_frames(baseline_payload)
        reform_frames = deserialize_simulation_frames(reform_payload)
        uk_local_authority_metadata = validate_uk_local_authority_metadata(
            self.report.baseline.geography.country,
            deserialize_uk_local_authority_metadata(baseline_payload),
            deserialize_uk_local_authority_metadata(reform_payload),
            region_code=self.report.baseline.geography.region,
        )
        validate_output_frames(baseline_frames, output_plan)
        validate_output_frames(reform_frames, output_plan)
        return (
            RetainedSimulationOutput(baseline_frames, baseline),
            RetainedSimulationOutput(reform_frames, reform),
            uk_local_authority_metadata,
        )

    def _aggregate_and_complete(
        self,
        parent: ComparisonReportRecord,
        output_plan: Stage12OutputPlan,
        descriptors: dict[str, SimulationArtifactDescriptor],
    ) -> dict[str, Any]:
        """Aggregate both outputs, store the aggregate and complete the parent.

        A shadow run's result is then compared with the production result.
        """
        baseline, reform, uk_local_authority_metadata = self._read_retained_outputs(
            output_plan, descriptors["baseline"], descriptors["reform"]
        )
        with self.span(Stage.STAGE12_AGGREGATION):
            aggregate = self.dependencies.aggregator(
                report=self.report,
                baseline=baseline,
                reform=reform,
                uk_local_authority_metadata=uk_local_authority_metadata,
            )
        with self.span(Stage.STAGE12_AGGREGATE_ARTIFACT_WRITE):
            aggregate_artifact = self.artifact_storage.write_aggregate(
                prefix=self.context.artifact_prefix,
                payload=aggregate,
            )
        descriptor = AggregateReportArtifactDescriptor(
            evaluation_id=self.report.evaluation_id,
            artifact=aggregate_artifact,
            baseline_artifact_sha256=baseline.descriptor.artifact.content_sha256,
            reform_artifact_sha256=reform.descriptor.artifact.content_sha256,
            bundle=self.report.baseline.bundle,
        )
        completed = datetime.now(UTC)
        completed_parent = self.persistence.replace_report(
            parent.model_copy(
                update={
                    "status": ComparisonRunLifecycleStatus.SUCCEEDED,
                    "aggregation_status": ComparisonRunAggregationStatus.SUCCEEDED,
                    "aggregate_output_uri": aggregate_artifact.uri,
                    "aggregate_output_sha256": aggregate_artifact.content_sha256,
                    "aggregate_schema_version": descriptor.aggregate_schema_version,
                    "updated_at": completed,
                    "completed_at": completed,
                }
            )
        )
        # After the Stage 12 report: a call Modal restarts between the two
        # writes finds that report succeeded and completes the report run
        # from its stored aggregate.
        self.complete_report_run(aggregate)
        compare_completed_report(
            parent=completed_parent,
            aggregate=aggregate,
            context=self.context,
            services=ComparisonServices(
                store=self.persistence,
                artifacts=self.artifact_storage,
                invoker=self.child_invoker,
                runtime=self.runtime,
                stage_plan=self.stage_plan,
            ),
        )
        return descriptor.model_dump(mode="json")

    def complete_report_run(self, aggregate: Mapping[str, Any]) -> None:
        """Store the aggregate's result with the report run's success.

        A report run this call no longer holds is logged and left as it is.
        A call that names no report run records nothing. Both simulation runs
        are given their child records' outcomes first, so a succeeded report
        run never combines a simulation run still running.
        """
        if self.report_run is None:
            return
        self.reconcile_simulation_runs()
        written = self.report_run.complete(
            aggregate["result"], country=self.report.baseline.geography.country
        )
        if not written.changed and written.status is not RunStatus.SUCCEEDED:
            logger.warning(
                "Stage 12 report %s finished, but its report run %s is %s",
                self.report.evaluation_id,
                self.report_run.run_ids.report_run_id,
                written.status.value,
            )

    def reconcile_simulation_runs(self) -> None:
        """Give each simulation run of the report its child record's outcome.

        A reused run already succeeded, and nothing writes to it.
        """
        if not isinstance(self.persistence, SimulationRunRecordingStore):
            return
        for simulation in (self.report.baseline, self.report.reform):
            if simulation.role.value not in self.reused:
                self.persistence.reconcile_simulation_run(
                    simulation.simulation_execution_id
                )

    def fail_running_children(self, detail: Stage12FailureDetail) -> None:
        """Fail every child record of a report run that is still running.

        A restarted call that fails before it starts a child again leaves
        that child's record, and so its simulation run, running under the
        call its pre-empted attempt cancelled. Each write is conditional on
        the record still running, and is mirrored onto the simulation run.
        """
        if not isinstance(self.persistence, SimulationRunRecordingStore):
            return
        failed_at = datetime.now(UTC)
        for simulation in (self.report.baseline, self.report.reform):
            try:
                record = self.persistence.get_simulation(
                    simulation.simulation_execution_id
                )
            except LookupError:
                continue
            if record.status is not ComparisonRunLifecycleStatus.RUNNING:
                continue
            self.persistence.replace_simulation_if_status(
                record.model_copy(
                    update={
                        "status": ComparisonRunLifecycleStatus.FAILED,
                        "error_code": detail.error_code,
                        "error_summary": detail.error_summary,
                        "updated_at": failed_at,
                        "completed_at": failed_at,
                    }
                ),
                expected_status=ComparisonRunLifecycleStatus.RUNNING,
            )

    def fail(self, parent: ComparisonReportRecord, error: Exception) -> None:
        """Record a failed run once, stop its unfinished children, fail the parent.

        Child, dispatch and wait-timeout failures raise Stage12ExecutionError
        only after reporting the original failure and stopping their unfinished
        peers. Any other failure is reported here, once, before any database
        write. If it occurred after a child started, such as a failed
        placeholder write or invocation attach, every started, unfinished child
        is then stopped so none keeps running for a failed report. A call that
        names a report run then fails the report's child records still
        running, gives both simulation runs their records' outcomes, and fails
        the report run, after the Stage 12 report.
        """
        if isinstance(error, Stage12ExecutionError):
            detail = error.detail
        else:
            detail = report_failure(
                error,
                FailureSite(
                    scope="report_coordination",
                    default_code="report_coordination_failed",
                    attributes={"evaluation_id": str(self.report.evaluation_id)},
                ),
                runtime=self.runtime,
                progress=self.progress,
            )
            cancel_pending_simulations(
                calls=self.calls,
                simulations=self.simulations,
                roles=set(self.calls) - set(self.descriptors),
                store=self.persistence,
                detail=cancellation("the coordinator failed"),
            )
        record_failure_cause(self.runtime, error, detail.error_summary)
        failed_at = datetime.now(UTC)
        persist_failure_record(
            partial(
                self.persistence.replace_report,
                parent.model_copy(
                    update={
                        "status": ComparisonRunLifecycleStatus.FAILED,
                        "aggregation_status": ComparisonRunAggregationStatus.FAILED,
                        "error_code": detail.error_code,
                        "error_summary": detail.error_summary,
                        "updated_at": failed_at,
                        "completed_at": failed_at,
                    }
                ),
            ),
            runtime=self.runtime,
            failure_scope="report_coordination",
        )
        # After the Stage 12 report, as on success: a call Modal restarts
        # between the two writes finds that report failed and runs it again.
        if self.report_run is not None:
            persist_failure_record(
                partial(
                    self.fail_running_children,
                    cancellation("the report failed"),
                ),
                runtime=self.runtime,
                failure_scope="report_coordination",
            )
            persist_failure_record(
                self.reconcile_simulation_runs,
                runtime=self.runtime,
                failure_scope="report_coordination",
            )
            persist_failure_record(
                partial(self.report_run.fail, detail),
                runtime=self.runtime,
                failure_scope="report_coordination",
            )
