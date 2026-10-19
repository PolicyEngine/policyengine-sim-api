"""Coordination of a Stage 12 report's baseline and reform simulations."""

from __future__ import annotations

import dataclasses
import logging
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
    ResultComparisonStatus,
    SimulationArtifactDescriptor,
    SimulationRole,
    Stage12InvocationContext,
    Stage12OutputPlan,
)
from policyengine_simulation_observability.stages import (
    STAGE12_CANONICAL_REPORT_STAGES,
    STAGE12_SHADOW_REPORT_STAGES,
    Stage,
    StagePlan,
)

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
from .dependencies import (
    ChildCall,
    ChildInvoker,
    ChildSpawnRequest,
    ComparisonStore,
    ModalChildInvoker,
    artifact_store,
    runtime_store,
)
from .failure_causes import (
    StageProgress,
    StageTracking,
    record_failure_cause,
    tracked_stage,
)
from .failure_recording import (
    persist_failure_record,
    report_failure,
    report_original_failure,
)
from .failures import (
    Stage12ExecutionError,
    Stage12FailureDetail,
    failure_detail_from_record,
)
from .output_planning import (
    plan_simulation_input,
    resolve_report_output_plan,
    validate_output_frames,
)
from .partition import should_segment_simulation
from .simulation import descriptor_from_record, simulation_input_sha256

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from contextlib import AbstractContextManager

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata

SIMULATION_POLL_INITIAL_SECONDS = 0.25
SIMULATION_POLL_MAX_SECONDS = 1.0
SIMULATION_POLL_BACKOFF_FACTOR = 2.0

logger = logging.getLogger(__name__)


def _next_poll_interval(current: float) -> float:
    return min(
        current * SIMULATION_POLL_BACKOFF_FACTOR,
        SIMULATION_POLL_MAX_SECONDS,
    )


def _log_child_operation_failure(
    level: int,
    message: str,
    *,
    simulation: PlannedSimulationExecutionInput,
    error: Exception,
) -> None:
    """Log a failed child state update or cancellation with its identifiers.

    ``message`` receives the simulation role, simulation execution ID,
    evaluation ID and error type, in that order. The error's own message is
    never logged.
    """
    error_type = type(error).__name__
    logger.log(
        level,
        message,
        simulation.role.value,
        simulation.simulation_execution_id,
        simulation.evaluation_id,
        error_type,
        extra={
            "evaluation_id": str(simulation.evaluation_id),
            "simulation_execution_id": str(simulation.simulation_execution_id),
            "simulation_role": simulation.role.value,
            "error_type": error_type,
        },
    )


def _replace_running_simulation(
    *,
    store: ComparisonStore,
    simulation: PlannedSimulationExecutionInput,
    status: ComparisonRunLifecycleStatus,
    detail: Stage12FailureDetail,
) -> ComparisonSimulationRecord:
    changed_at = datetime.now(UTC)
    current = store.get_simulation(simulation.simulation_execution_id)
    candidate = current.model_copy(
        update={
            "status": status,
            "error_code": detail.error_code,
            "error_summary": detail.error_summary,
            "updated_at": changed_at,
            "completed_at": changed_at,
        }
    )
    result, _ = store.replace_simulation_if_status(
        candidate,
        expected_status=ComparisonRunLifecycleStatus.RUNNING,
    )
    return result


SIMULATION_CANCELLED_ERROR_CODE = "simulation_cancelled"


def _cancellation(reason: str) -> Stage12FailureDetail:
    """Describe a child the coordinator stopped; ``reason`` ends the summary."""
    return Stage12FailureDetail(
        error_code=SIMULATION_CANCELLED_ERROR_CODE,
        error_summary=f"Cancelled after {reason}.",
    )


def _cancel_pending_simulations(
    *,
    calls: dict[str, ChildCall],
    simulations: dict[str, PlannedSimulationExecutionInput],
    roles: set[str],
    store: ComparisonStore,
    detail: Stage12FailureDetail,
) -> None:
    """Record each started, unfinished child as failed, then stop it.

    Only a running record changes, so a child that has already succeeded or
    failed keeps its own outcome. An unsegmented child is always cancelled
    through Modal, even if its record could not be written. State-update and
    cancellation errors are logged rather than raised, so they never replace
    the failure that caused the cancellation.
    """
    for role in sorted(roles):
        simulation = simulations[role]
        try:
            _replace_running_simulation(
                store=store,
                simulation=simulation,
                status=ComparisonRunLifecycleStatus.FAILED,
                detail=detail,
            )
        except Exception as error:  # noqa: BLE001  # a failed cancellation write is logged and must not replace the failure that caused it
            _log_child_operation_failure(
                logging.ERROR,
                "Stage 12 could not record cancellation of %s simulation %s "
                "in report %s (%s)",
                simulation=simulation,
                error=error,
            )
            cancellation_recorded = False
        else:
            cancellation_recorded = True
        if cancellation_recorded and should_segment_simulation(simulation):
            # A segmented worker reads its record, cancels the region-group
            # calls it started, and exits. Modal does not cancel spawned calls
            # recursively, so cancelling the worker through Modal would leave
            # those calls running. That is the fallback only when the record
            # could not be written and the worker cannot learn of the
            # cancellation.
            continue
        try:
            calls[role].cancel()
        except Exception as error:  # noqa: BLE001  # a failed Modal cancellation is logged and must not replace the failure that caused it
            _log_child_operation_failure(
                logging.WARNING,
                "Stage 12 could not cancel %s simulation %s in report %s (%s)",
                simulation=simulation,
                error=error,
            )


def _simulation_failure_attributes(
    simulation: PlannedSimulationExecutionInput,
) -> dict[str, object]:
    return {
        "simulation_role": simulation.role.value,
        "evaluation_id": str(simulation.evaluation_id),
        "simulation_execution_id": str(simulation.simulation_execution_id),
    }


def _log_child_state_failure(
    *,
    simulation: PlannedSimulationExecutionInput,
    error: Exception,
) -> None:
    _log_child_operation_failure(
        logging.ERROR,
        "Stage 12 could not record the failure of %s simulation %s in report %s (%s)",
        simulation=simulation,
        error=error,
    )


def _record_child_failure(
    *,
    store: ComparisonStore,
    simulation: PlannedSimulationExecutionInput,
    detail: Stage12FailureDetail,
) -> None:
    """Record a running child as failed with its own cause.

    A child that has already finished keeps its own outcome. A state error is
    logged rather than raised, so it never replaces the child's failure.
    """
    try:
        _replace_running_simulation(
            store=store,
            simulation=simulation,
            status=ComparisonRunLifecycleStatus.FAILED,
            detail=detail,
        )
    except Exception as state_error:  # noqa: BLE001  # a failed state write is logged and must not replace the child's failure
        _log_child_state_failure(simulation=simulation, error=state_error)


def _child_failure_detail(
    *,
    error: Exception,
    simulation: PlannedSimulationExecutionInput,
    store: ComparisonStore,
    runtime: ObservabilityRuntime | None,
    progress: StageProgress,
) -> Stage12FailureDetail:
    """Report a failed child call and return the failure for its report.

    A failure the child's worker recorded itself takes precedence over the
    error its call raised. Reading or writing the child's record can fail,
    for example when the database is unavailable. That error is logged, and
    the call's own failure is used and still recorded where possible, so a
    state error never replaces the child's failure.
    """
    detail = report_failure(
        error,
        runtime=runtime,
        failure_scope="child_wait",
        default_code="simulation_invocation_failed",
        attributes=_simulation_failure_attributes(simulation),
        progress=progress,
    )
    try:
        child = store.get_simulation(simulation.simulation_execution_id)
    except Exception as state_error:  # noqa: BLE001  # if the child's record cannot be read, its call failure is reported instead
        _log_child_state_failure(simulation=simulation, error=state_error)
    else:
        if (
            child.status is ComparisonRunLifecycleStatus.FAILED
            and child.error_code is not None
        ):
            return failure_detail_from_record(
                error_code=child.error_code,
                error_summary=child.error_summary,
            )
    _record_child_failure(store=store, simulation=simulation, detail=detail)
    return detail


def _child_record(
    *,
    simulation: PlannedSimulationExecutionInput,
    context: Stage12InvocationContext,
    function_name: str,
) -> ComparisonSimulationRecord:
    return ComparisonSimulationRecord(
        simulation_execution_id=simulation.simulation_execution_id,
        evaluation_id=simulation.evaluation_id,
        role=simulation.role,
        input_sha256=simulation_input_sha256(simulation),
        worker_version=context.worker_version,
        modal_application=context.modal_application,
        simulation_callable=function_name,
        version_manifest_sha256=context.version_manifest_sha256,
        status=ComparisonRunLifecycleStatus.PENDING,
        created_at=context.created_at,
        updated_at=context.created_at,
        retention_expires_at=context.retention_expires_at,
    )


def _validate_dispatch_context(
    report: ReportExecutionInput,
    context: Stage12InvocationContext,
    application_name: str,
) -> None:
    """Reject a dispatch context naming another application, worker or bundle."""
    if application_name != context.modal_application:
        msg = "report coordinator application differs from dispatch context"
        raise ValueError(msg)
    if report.baseline.bundle.policyengine_version != context.worker_version:
        msg = "report worker version differs from dispatch context"
        raise ValueError(msg)
    if report.baseline.bundle.bundle_manifest_sha256 != context.bundle_manifest_sha256:
        msg = "report bundle digest differs from dispatch context"
        raise ValueError(msg)


def _validate_submitted_parent(
    *,
    report: ReportExecutionInput,
    context: Stage12InvocationContext,
    parent: ComparisonReportRecord,
) -> None:
    """Reject dispatch metadata that does not describe this exact report."""
    _validate_parent_matches_invocation(report=report, context=context, parent=parent)
    _validate_parent_is_unclaimed(parent)
    _validate_parent_production_link(report=report, context=context, parent=parent)


def _validate_parent_matches_invocation(
    *,
    report: ReportExecutionInput,
    context: Stage12InvocationContext,
    parent: ComparisonReportRecord,
) -> None:
    """Require every parent field the dispatch fixes to match the report and context."""
    bundle = report.baseline.bundle
    expected = {
        "evaluation_id": report.evaluation_id,
        "environment": context.environment,
        "originating_request_id": context.request_id,
        "worker_version": context.worker_version,
        "modal_application": context.modal_application,
        "version_manifest_sha256": context.version_manifest_sha256,
        "policyengine_version": bundle.policyengine_version,
        "country_package_name": bundle.country_package_name,
        "country_package_version": bundle.country_package_version,
        "country": report.baseline.geography.country,
        "dataset_identity": bundle.dataset.identity,
        "dataset_uri": bundle.dataset.uri,
        "data_package_name": bundle.dataset.data_package_name,
        "data_package_version": bundle.dataset.data_package_version,
        "data_artifact_revision": bundle.dataset.artifact_revision,
        "created_at": context.created_at,
        "retention_expires_at": context.retention_expires_at,
    }
    for field, value in expected.items():
        if getattr(parent, field) != value:
            msg = f"submitted parent {field} does not match invocation"
            raise ValueError(msg)


def _validate_parent_is_unclaimed(parent: ComparisonReportRecord) -> None:
    """Require a pending parent that no coordinator has started."""
    if parent.status is not ComparisonRunLifecycleStatus.PENDING:
        msg = "submitted parent must be pending"
        raise ValueError(msg)
    if parent.aggregation_status is not ComparisonRunAggregationStatus.NOT_STARTED:
        msg = "submitted parent aggregation must not be started"
        raise ValueError(msg)
    if parent.coordinator_invocation_id is not None:
        msg = "submitted parent must not contain an invocation identifier"
        raise ValueError(msg)


def _validate_parent_production_link(
    *,
    report: ReportExecutionInput,
    context: Stage12InvocationContext,
    parent: ComparisonReportRecord,
) -> None:
    """Require a direct parent to stand alone and a shadow parent to name its job."""
    production_job_id = context.production_function_call_id
    if production_job_id is None:
        if parent.production_identity != f"direct:{report.evaluation_id}":
            msg = "direct parent identity does not match its report"
            raise ValueError(msg)
        if parent.incumbent_execution_id is not None:
            msg = "direct parent must not name a production execution"
            raise ValueError(msg)
        if parent.comparison_status is not ResultComparisonStatus.NOT_REQUESTED:
            msg = "direct parent must not request result comparison"
            raise ValueError(msg)
    elif (
        parent.production_identity != production_job_id
        or parent.incumbent_execution_id != production_job_id
    ):
        msg = "automatic parent does not match the production execution"
        raise ValueError(msg)


def _claim_parent(
    *,
    submitted: ComparisonReportRecord,
    coordinator_invocation_id: str,
    store: ComparisonStore,
) -> tuple[ComparisonReportRecord, bool]:
    """Create the parent in Modal or claim a retry; reject duplicate execution."""
    coordinating_at = datetime.now(UTC)
    candidate = submitted.model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.RUNNING,
            "aggregation_status": ComparisonRunAggregationStatus.RUNNING,
            "coordinator_invocation_id": coordinator_invocation_id,
            "error_code": None,
            "error_summary": None,
            "started_at": coordinating_at,
            "updated_at": coordinating_at,
            "completed_at": None,
        }
    )
    persistence = store.create_or_resolve_report(candidate)
    parent = persistence.record
    if persistence.created:
        return parent, True
    if parent.evaluation_id != submitted.evaluation_id:
        msg = "existing comparison identity has a different report identifier"
        raise ValueError(msg)
    if parent.status in {
        ComparisonRunLifecycleStatus.RUNNING,
        ComparisonRunLifecycleStatus.SUCCEEDED,
        ComparisonRunLifecycleStatus.SKIPPED,
    }:
        return parent, False
    claimed = store.replace_report(
        parent.model_copy(
            update={
                "status": ComparisonRunLifecycleStatus.RUNNING,
                "aggregation_status": ComparisonRunAggregationStatus.RUNNING,
                "coordinator_invocation_id": coordinator_invocation_id,
                "error_code": None,
                "error_summary": None,
                "started_at": parent.started_at or coordinating_at,
                "updated_at": coordinating_at,
                "completed_at": None,
            }
        )
    )
    return claimed, True


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
    plan resolution, no telemetry runtime, the real clock and sleep, and the
    standard child wait window.
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


@dataclass
class _ReportCoordination:
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
        """Plan, start and await both children, then aggregate and complete."""
        output_plan, planned_simulations = self._prepare(parent)
        self.simulations = {
            simulation.role.value: simulation for simulation in planned_simulations
        }
        children = self._create_children(planned_simulations)
        descriptors, calls = self._start_children(planned_simulations, children)
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
        return output_plan, tuple(simulations)

    def _create_children(
        self, planned_simulations: tuple[PlannedSimulationExecutionInput, ...]
    ) -> dict[SimulationRole, ComparisonSimulationRecord]:
        """Create each child's row, or resolve the row an earlier attempt made."""
        with self.span(Stage.STAGE12_CHILD_STATE_CREATE):
            return {
                simulation.role: self.persistence.create_or_resolve_simulation(
                    _child_record(
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
                calls[role] = self.child_invoker.restore(child.modal_invocation_id)
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
                        context=self.context.model_dump(mode="json"),
                        observability_context=child_observability_context,
                    )
                )
        except Exception as error:  # noqa: BLE001  # any dispatch failure is recorded on the child and stops its started peers
            detail = report_failure(
                error,
                runtime=self.runtime,
                failure_scope="child_dispatch",
                default_code="simulation_dispatch_failed",
                attributes=_simulation_failure_attributes(simulation),
                progress=self.progress,
            )
            _record_child_failure(
                store=self.persistence,
                simulation=simulation,
                detail=detail,
            )
            _cancel_pending_simulations(
                calls=self.calls,
                simulations=self.simulations,
                roles=set(self.calls),
                store=self.persistence,
                detail=_cancellation(f"the {simulation.role.value} simulation failed"),
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
                    detail = _child_failure_detail(
                        error=error,
                        simulation=self.simulations[role],
                        store=self.persistence,
                        runtime=self.runtime,
                        progress=self.progress,
                    )
                    _cancel_pending_simulations(
                        calls=calls,
                        simulations=self.simulations,
                        roles=pending - {role},
                        store=self.persistence,
                        detail=_cancellation(f"the {role} simulation failed"),
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
                _cancel_pending_simulations(
                    calls=calls,
                    simulations=self.simulations,
                    roles=pending,
                    store=self.persistence,
                    detail=_cancellation(
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
            baseline_payload = self.artifact_storage.read(baseline.artifact.uri)
            reform_payload = self.artifact_storage.read(reform.artifact.uri)
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

    def fail(self, parent: ComparisonReportRecord, error: Exception) -> None:
        """Record a failed run once, stop its unfinished children, fail the parent.

        Child, dispatch and wait-timeout failures raise Stage12ExecutionError
        only after reporting the original failure and stopping their unfinished
        peers. Any other failure is reported here, once, before any database
        write. If it occurred after a child started, such as a failed
        placeholder write or invocation attach, every started, unfinished child
        is then stopped so none keeps running for a failed report.
        """
        if isinstance(error, Stage12ExecutionError):
            detail = error.detail
        else:
            detail = report_failure(
                error,
                runtime=self.runtime,
                failure_scope="report_coordination",
                default_code="report_coordination_failed",
                attributes={"evaluation_id": str(self.report.evaluation_id)},
                progress=self.progress,
            )
            _cancel_pending_simulations(
                calls=self.calls,
                simulations=self.simulations,
                roles=set(self.calls) - set(self.descriptors),
                store=self.persistence,
                detail=_cancellation("the coordinator failed"),
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


def coordinate_report(
    payload: object,
    context_payload: object,
    parent_payload: object,
    *,
    invocation: CoordinatorInvocation,
    dependencies: CoordinatorDependencies | None = None,
) -> dict[str, Any]:
    """Run one Stage 12 comparison report end to end; return its aggregate descriptor.

    Claims the parent report (one already running or finished returns a
    ``deduplicated`` summary instead), starts or reattaches the baseline and
    reform child simulations within one shared wait window, and aggregates
    their digest-checked artifacts; shadow runs are then compared with the
    production result. A dispatch context that disagrees with the report
    raises ``ValueError`` before the claim; any later failure stops the
    unfinished children, marks the report FAILED and raises a
    ``RuntimeError`` that carries no calculation detail.
    """
    dependencies = dependencies or CoordinatorDependencies()
    # Open the shared child-wait window before any setup work.
    child_wait_deadline = dependencies.monotonic() + dependencies.wait_timeout_seconds
    report = ReportExecutionInput.model_validate(payload)
    context = Stage12InvocationContext.model_validate(context_payload)
    submitted_parent = ComparisonReportRecord.model_validate(parent_payload)
    _validate_dispatch_context(report, context, invocation.application_name)
    _validate_submitted_parent(
        report=report,
        context=context,
        parent=submitted_parent,
    )
    coordination = _ReportCoordination(
        report=report,
        context=context,
        invocation=invocation,
        persistence=dependencies.store or runtime_store(dependencies.runtime),
        artifact_storage=dependencies.artifacts or artifact_store(),
        child_invoker=dependencies.invoker or ModalChildInvoker(),
        dependencies=dependencies,
        stage_plan=(
            STAGE12_SHADOW_REPORT_STAGES
            if context.production_function_call_id is not None
            else STAGE12_CANONICAL_REPORT_STAGES
        ),
        progress=StageProgress(),
        child_wait_deadline=child_wait_deadline,
    )
    with coordination.span(Stage.STAGE12_COORDINATOR_CLAIM):
        parent, should_execute = _claim_parent(
            submitted=submitted_parent,
            coordinator_invocation_id=invocation.invocation_id,
            store=coordination.persistence,
        )
    if not should_execute:
        return {
            "deduplicated": True,
            "evaluation_id": str(parent.evaluation_id),
            "status": parent.status.value,
        }
    try:
        return coordination.run(parent)
    # Persist a redacted cause for any coordinator, child-call, aggregation, or
    # artifact failure before returning a stable exception to Modal.
    except Exception as error:  # noqa: BLE001  # every failure is persisted before a stable exception returns to Modal
        coordination.fail(parent, error)
        msg = "Stage 12 report coordination failed"
        raise RuntimeError(msg) from None
