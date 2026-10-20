"""Coordination of a Stage 12 report's baseline and reform simulations."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_contract.report_execution import (
    ReportExecutionError,
    ReportExecutionProvenance,
    ReportExecutionResult,
    report_aggregate_sha256,
)
from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactDescriptor,
    AggregateReportArtifactPayload,
    ComparisonReportRecord,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    PlannedSimulationExecutionInput,
    ReportExecutionInput,
    ReportResultDelivery,
    ResultComparisonStatus,
    SimulationArtifactDescriptor,
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
    comparison_run_prefix,
    deserialize_calculation_provenance,
    deserialize_simulation_frames,
    deserialize_uk_local_authority_metadata,
)
from policyengine_simulation_executor.stage12_timeouts import (
    SIMULATION_WAIT_TIMEOUT_SECONDS,
)

from .aggregation import (
    build_aggregate_report,
    validate_aligned_outputs,
    validate_uk_local_authority_metadata,
)
from .comparison import compare_completed_report
from .dependencies import (
    ChildCall,
    ChildInvoker,
    ComparisonStore,
    ModalChildInvoker,
    artifact_store,
    runtime_store,
)
from .failure_causes import StageProgress, record_failure_cause, tracked_stage
from .failure_recording import (
    persist_failure_record,
    report_failure,
    report_original_failure,
)
from .failures import (
    Stage12ExecutionError,
    Stage12FailureDetail,
    failure_detail_from_record,
    failure_detail_from_returned_error,
    report_execution_error,
)
from .output_planning import (
    plan_simulation_input,
    resolve_report_output_plan,
    validate_output_frames,
)
from .partition import should_segment_simulation
from .simulation import descriptor_from_record, simulation_input_sha256

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    import pandas as pd
    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.uk_geography import UKLocalAuthorityMetadata

    from .output_planning import StageScope

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


def _returned_child_failure_detail(
    *,
    error: Exception,
    simulation: PlannedSimulationExecutionInput,
    runtime: ObservabilityRuntime | None,
    progress: StageProgress,
) -> Stage12FailureDetail:
    """Report a returned report's failed child call; return its report's failure.

    The failure the child raised as ``ReportExecutionError`` takes precedence
    over the redacted call error, as a Stage 12 child's recorded failure does.
    """
    detail = report_failure(
        error,
        runtime=runtime,
        failure_scope="child_wait",
        default_code="simulation_invocation_failed",
        attributes=_simulation_failure_attributes(simulation),
        progress=progress,
    )
    if isinstance(error, ReportExecutionError):
        return failure_detail_from_returned_error(error)
    return detail


def _cancel_returned_simulations(
    *,
    calls: dict[str, ChildCall],
    simulations: dict[str, PlannedSimulationExecutionInput],
    roles: set[str],
    detail: Stage12FailureDetail | None = None,
) -> None:
    """Stop each started, unfinished child of a returned report through Modal.

    No record exists to carry ``detail`` or to tell a segmented child to stop,
    so every child is cancelled through Modal. Modal does not cancel the
    region-group segment calls a segmented child started; they run until they
    finish or time out. A cancellation error is logged rather than raised, so
    it never replaces the failure that caused the cancellation.
    """
    del detail
    for role in sorted(roles):
        try:
            calls[role].cancel()
        except Exception as error:  # noqa: BLE001  # a failed Modal cancellation is logged and must not replace the failure that caused it
            _log_child_operation_failure(
                logging.WARNING,
                "Stage 12 could not cancel %s simulation %s in report %s (%s)",
                simulation=simulations[role],
                error=error,
            )


class _ChildFailureHandler(Protocol):
    """Report a failed child call and return the failure its report records.

    The Stage 12 path reads and records the child's row; a returned report
    uses the ``ReportExecutionError`` its child raised.
    """

    def __call__(
        self,
        *,
        error: Exception,
        simulation: PlannedSimulationExecutionInput,
    ) -> Stage12FailureDetail:
        """Return the report's failure for ``simulation``'s failed call."""
        ...


class _ChildCanceller(Protocol):
    """Stop the started, unfinished children with the given roles.

    ``detail`` is the cancellation a Stage 12 child's row records; a returned
    report's children have no row to record it on.
    """

    def __call__(self, *, roles: set[str], detail: Stage12FailureDetail) -> None:
        """Stop each child named in ``roles``."""
        ...


def _observe_simulations(
    *,
    calls: dict[str, ChildCall],
    simulations: dict[str, PlannedSimulationExecutionInput],
    descriptors: dict[str, SimulationArtifactDescriptor],
    child_failure_detail: _ChildFailureHandler,
    cancel_simulations: _ChildCanceller,
    runtime: ObservabilityRuntime | None,
    progress: StageProgress,
    wait_stage_name: str,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    deadline: float,
    timeout_seconds: float,
) -> None:
    """Wait for every started child within the shared window, or stop them all.

    ``child_failure_detail`` reports a failed child and returns the report's
    failure; ``cancel_simulations`` stops the children still running.
    """
    pending = set(calls)
    poll_interval = SIMULATION_POLL_INITIAL_SECONDS

    while pending:
        progress_made = False
        for role in sorted(pending):
            call = calls[role]
            try:
                # The probe records the child's role, so a failure names it.
                with progress.stage(wait_stage_name, role=role):
                    result = call.get(timeout=0)
                    descriptor = SimulationArtifactDescriptor.model_validate(result)
            except TimeoutError:
                continue
            except Exception as error:  # noqa: BLE001  # a child call's failure can be any exception type; it is recorded and stops the other child
                detail = child_failure_detail(
                    error=error,
                    simulation=simulations[role],
                )
                cancel_simulations(
                    roles=pending - {role},
                    detail=_cancellation(f"the {role} simulation failed"),
                )
                raise Stage12ExecutionError(detail) from None
            descriptors[role] = descriptor
            pending.remove(role)
            progress_made = True

        if not pending:
            return
        remaining_seconds = deadline - monotonic()
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
                runtime=runtime,
                failure_scope="child_wait_timeout",
            )
            cancel_simulations(
                roles=pending,
                detail=_cancellation(
                    f"the {timeout_seconds:g}-second wait window closed"
                ),
            )
            raise wait_timeout
        # Never sleep past the deadline, so the window closes on time.
        sleep(min(poll_interval, remaining_seconds))
        poll_interval = (
            SIMULATION_POLL_INITIAL_SECONDS
            if progress_made
            else _next_poll_interval(poll_interval)
        )


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


def _validate_submitted_parent(
    *,
    report: ReportExecutionInput,
    context: Stage12InvocationContext,
    parent: ComparisonReportRecord,
) -> None:
    """Reject dispatch metadata that does not describe this exact report."""
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
    if parent.status is not ComparisonRunLifecycleStatus.PENDING:
        msg = "submitted parent must be pending"
        raise ValueError(msg)
    if parent.aggregation_status is not ComparisonRunAggregationStatus.NOT_STARTED:
        msg = "submitted parent aggregation must not be started"
        raise ValueError(msg)
    if parent.coordinator_invocation_id is not None:
        msg = "submitted parent must not contain an invocation identifier"
        raise ValueError(msg)
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


def _validate_dispatch(
    *,
    report: ReportExecutionInput,
    context: Stage12InvocationContext,
    application_name: str,
) -> None:
    """Reject a dispatch whose application, worker or bundle differs from the report."""
    if application_name != context.modal_application:
        msg = "report coordinator application differs from dispatch context"
        raise ValueError(msg)
    if report.baseline.bundle.policyengine_version != context.worker_version:
        msg = "report worker version differs from dispatch context"
        raise ValueError(msg)
    if report.baseline.bundle.bundle_manifest_sha256 != context.bundle_manifest_sha256:
        msg = "report bundle digest differs from dispatch context"
        raise ValueError(msg)


def _child_context_payload(context: Stage12InvocationContext) -> dict[str, Any]:
    """Return the dispatch context a child simulation receives.

    A Stage 12 child receives the fields it received before
    ``result_delivery`` existed, so a worker deployed without that field still
    accepts the context. A returned report's child needs the field, so that
    it writes no rows either.
    """
    if context.result_delivery is ReportResultDelivery.STAGE12_DATABASE:
        return context.model_dump(mode="json", exclude={"result_delivery"})
    return context.model_dump(mode="json")


def _plan_simulations(
    report: ReportExecutionInput,
    *,
    output_plan_resolver: Callable[[ReportExecutionInput], Stage12OutputPlan] | None,
    stage_scope: StageScope,
) -> tuple[Stage12OutputPlan, tuple[PlannedSimulationExecutionInput, ...]]:
    """Resolve the report's shared output plan and attach it to both inputs."""
    with stage_scope(
        Stage.STAGE12_OUTPUT_PLANNING,
        {"country": report.baseline.geography.country},
    ):
        if output_plan_resolver is None:
            output_plan = resolve_report_output_plan(
                report,
                stage_scope=stage_scope,
            )
        else:
            output_plan = output_plan_resolver(report)
    planned_inputs: list[PlannedSimulationExecutionInput] = []
    for simulation in (report.baseline, report.reform):
        with stage_scope(
            Stage.STAGE12_CHILD_INPUT_PLANNING,
            {"simulation_role": simulation.role.value},
        ):
            planned_inputs.append(plan_simulation_input(simulation, output_plan))
    return output_plan, tuple(planned_inputs)


@dataclass(frozen=True)
class _SimulationOutputs:
    """The checked baseline and reform outputs one report aggregates."""

    baseline: SimulationArtifactDescriptor
    reform: SimulationArtifactDescriptor
    baseline_frames: dict[str, pd.DataFrame]
    reform_frames: dict[str, pd.DataFrame]
    uk_local_authority_metadata: UKLocalAuthorityMetadata | None

    def aggregate(
        self,
        report: ReportExecutionInput,
        aggregator: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        """Return ``aggregator``'s aggregate report payload for these outputs."""
        return aggregator(
            report=report,
            baseline_frames=self.baseline_frames,
            reform_frames=self.reform_frames,
            baseline_descriptor=self.baseline,
            reform_descriptor=self.reform,
            uk_local_authority_metadata=self.uk_local_authority_metadata,
        )


def _read_simulation_outputs(
    *,
    report: ReportExecutionInput,
    output_plan: Stage12OutputPlan,
    descriptors: dict[str, SimulationArtifactDescriptor],
    artifact_storage: Stage12ArtifactStore,
    stage_span: Callable[[Stage], AbstractContextManager[object]],
) -> _SimulationOutputs:
    """Read both children's artifacts and check they form this report's pair.

    Raises ``ValueError`` when the artifacts do not align, a digest does not
    match, the UK authority metadata disagrees, or a frame misses the plan.
    """
    baseline = descriptors["baseline"]
    reform = descriptors["reform"]
    validate_aligned_outputs(report, output_plan, baseline, reform)
    with stage_span(Stage.STAGE12_ARTIFACT_READ):
        baseline_payload = artifact_storage.read(baseline.artifact.uri)
        reform_payload = artifact_storage.read(reform.artifact.uri)
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
        report.baseline.geography.country,
        deserialize_uk_local_authority_metadata(baseline_payload),
        deserialize_uk_local_authority_metadata(reform_payload),
        region_code=report.baseline.geography.region,
    )
    validate_output_frames(baseline_frames, output_plan)
    validate_output_frames(reform_frames, output_plan)
    return _SimulationOutputs(
        baseline=baseline,
        reform=reform,
        baseline_frames=baseline_frames,
        reform_frames=reform_frames,
        uk_local_authority_metadata=uk_local_authority_metadata,
    )


def _returned_result(
    *,
    report: ReportExecutionInput,
    context: Stage12InvocationContext,
    output_plan: Stage12OutputPlan,
    outputs: _SimulationOutputs,
    aggregate: dict[str, Any],
    coordinator_invocation_id: str,
) -> ReportExecutionResult:
    """Check the aggregate and build the result a returned report delivers.

    The aggregate passes the checks a Stage 12 aggregate artifact write
    applies, must describe this report, and its result must be a
    ``SingleYearMacroOutput``.
    """
    payload = AggregateReportArtifactPayload.model_validate(aggregate)
    if (
        payload.evaluation_id != report.evaluation_id
        or payload.bundle != report.baseline.bundle
        or payload.requested_aggregates != report.requested_aggregates
    ):
        msg = "aggregate does not describe this report"
        raise ValueError(msg)
    macro_output = SingleYearMacroOutput.model_validate(payload.result)
    return ReportExecutionResult(
        evaluation_id=report.evaluation_id,
        requested_aggregates=report.requested_aggregates,
        aggregate=macro_output,
        aggregate_sha256=report_aggregate_sha256(macro_output),
        output_plan=output_plan,
        baseline=outputs.baseline,
        reform=outputs.reform,
        provenance=ReportExecutionProvenance(
            bundle=report.baseline.bundle,
            worker_version=context.worker_version,
            modal_environment=context.modal_environment,
            modal_application=context.modal_application,
            simulation_callable=context.simulation_callable,
            version_manifest_sha256=context.version_manifest_sha256,
            coordinator_invocation_id=coordinator_invocation_id,
        ),
        completed_at=datetime.now(UTC),
    )


def _coordinate_report_for_caller(
    report: ReportExecutionInput,
    context: Stage12InvocationContext,
    *,
    application_name: str,
    coordinator_invocation_id: str,
    artifact_storage: Stage12ArtifactStore,
    child_invoker: ChildInvoker,
    aggregator: Callable[..., dict[str, Any]],
    output_plan_resolver: Callable[[ReportExecutionInput], Stage12OutputPlan] | None,
    runtime: ObservabilityRuntime | None,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    child_wait_deadline: float,
    wait_timeout_seconds: float,
) -> dict[str, Any]:
    """Run one report and return its ``ReportExecutionResult``, writing no rows.

    Nothing claims the report, so a repeated dispatch runs it again; the
    caller deduplicates. Artifacts go under the Stage 12 layout for the
    context's environment, creation time and report. Both children start
    with the same delivery, so they write no rows either, and a child that
    must stop is cancelled through Modal. No aggregate artifact is written:
    the aggregate is returned inline. Every failure is reported once,
    recorded as the operation's cause, and raised as ``ReportExecutionError``
    with its redacted detail.
    """
    stage_plan = STAGE12_CANONICAL_REPORT_STAGES
    progress = StageProgress()
    stage_span = partial(
        tracked_stage,
        stage_plan=stage_plan,
        progress=progress,
        runtime=runtime,
    )
    stage_scope = partial(stage_span, operation=True)
    descriptors: dict[str, SimulationArtifactDescriptor] = {}
    calls: dict[str, ChildCall] = {}
    simulations: dict[str, PlannedSimulationExecutionInput] = {}
    cancel_simulations = partial(
        _cancel_returned_simulations,
        calls=calls,
        simulations=simulations,
    )
    try:
        with stage_scope(Stage.STAGE12_COORDINATOR_PREPARATION):
            context = context.model_copy(
                update={
                    "artifact_prefix": comparison_run_prefix(
                        environment=context.environment,
                        created_at=context.created_at,
                        evaluation_id=report.evaluation_id,
                    )
                }
            )
            output_plan, planned_simulations = _plan_simulations(
                report,
                output_plan_resolver=output_plan_resolver,
                stage_scope=stage_scope,
            )
        simulations.update(
            {simulation.role.value: simulation for simulation in planned_simulations}
        )
        for simulation in planned_simulations:
            try:
                with stage_span(
                    Stage.STAGE12_CHILD_DISPATCH,
                    {"simulation_role": simulation.role.value},
                ):
                    call = child_invoker.spawn(
                        application_name=application_name,
                        function_name=context.simulation_callable,
                        environment=context.modal_environment,
                        simulation=simulation.model_dump(mode="json"),
                        context=_child_context_payload(context),
                        observability_context=(
                            runtime.capture_context() if runtime is not None else None
                        ),
                    )
            except Exception as error:  # noqa: BLE001  # any dispatch failure stops the started peer and returns to the caller as a typed error
                detail = report_failure(
                    error,
                    runtime=runtime,
                    failure_scope="child_dispatch",
                    default_code="simulation_dispatch_failed",
                    attributes=_simulation_failure_attributes(simulation),
                    progress=progress,
                )
                cancel_simulations(roles=set(calls))
                raise Stage12ExecutionError(detail) from None
            calls[simulation.role.value] = call
        with stage_span(Stage.STAGE12_CHILD_WAIT):
            _observe_simulations(
                calls=calls,
                simulations=simulations,
                descriptors=descriptors,
                child_failure_detail=partial(
                    _returned_child_failure_detail,
                    runtime=runtime,
                    progress=progress,
                ),
                cancel_simulations=cancel_simulations,
                runtime=runtime,
                progress=progress,
                wait_stage_name=stage_plan.name(Stage.STAGE12_CHILD_WAIT),
                sleep=sleep,
                monotonic=monotonic,
                deadline=child_wait_deadline,
                timeout_seconds=wait_timeout_seconds,
            )
        outputs = _read_simulation_outputs(
            report=report,
            output_plan=output_plan,
            descriptors=descriptors,
            artifact_storage=artifact_storage,
            stage_span=stage_span,
        )
        with stage_span(Stage.STAGE12_AGGREGATION):
            result = _returned_result(
                report=report,
                context=context,
                output_plan=output_plan,
                outputs=outputs,
                aggregate=outputs.aggregate(report, aggregator),
                coordinator_invocation_id=coordinator_invocation_id,
            )
        return result.model_dump(mode="json")
    except Exception as error:  # noqa: BLE001  # every failure returns to the caller as a typed error with its redacted detail
        if isinstance(error, Stage12ExecutionError):
            # Child, dispatch and wait-timeout failures raise
            # Stage12ExecutionError only after reporting the original failure
            # and stopping their unfinished peers.
            detail = error.detail
        else:
            detail = report_failure(
                error,
                runtime=runtime,
                failure_scope="report_coordination",
                default_code="report_coordination_failed",
                attributes={"evaluation_id": str(report.evaluation_id)},
                progress=progress,
            )
            cancel_simulations(roles=set(calls) - set(descriptors))
        record_failure_cause(runtime, error, detail.error_summary)
        raise report_execution_error(report.evaluation_id, detail) from None


def coordinate_report(
    payload: object,
    context_payload: object,
    parent_payload: object | None = None,
    *,
    application_name: str,
    coordinator_invocation_id: str,
    store: ComparisonStore | None = None,
    artifacts: Stage12ArtifactStore | None = None,
    invoker: ChildInvoker | None = None,
    aggregator: Callable[..., dict[str, Any]] = build_aggregate_report,
    output_plan_resolver: Callable[[ReportExecutionInput], Stage12OutputPlan]
    | None = None,
    runtime: ObservabilityRuntime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    wait_timeout_seconds: float = SIMULATION_WAIT_TIMEOUT_SECONDS,
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

    When the context's ``result_delivery`` returns the report to its caller,
    ``parent_payload`` must be absent and ``store`` is not used: the report
    returns a ``ReportExecutionResult`` payload or raises
    ``ReportExecutionError`` (see ``_coordinate_report_for_caller``).
    """
    # Open the shared child-wait window before any setup work.
    child_wait_deadline = monotonic() + wait_timeout_seconds
    report = ReportExecutionInput.model_validate(payload)
    context = Stage12InvocationContext.model_validate(context_payload)
    if context.result_delivery is ReportResultDelivery.RETURN_TO_CALLER:
        if parent_payload is not None:
            msg = "a report returned to its caller takes no Stage 12 parent record"
            raise ValueError(msg)
        _validate_dispatch(
            report=report,
            context=context,
            application_name=application_name,
        )
        return _coordinate_report_for_caller(
            report,
            context,
            application_name=application_name,
            coordinator_invocation_id=coordinator_invocation_id,
            artifact_storage=artifacts or artifact_store(),
            child_invoker=invoker or ModalChildInvoker(),
            aggregator=aggregator,
            output_plan_resolver=output_plan_resolver,
            runtime=runtime,
            sleep=sleep,
            monotonic=monotonic,
            child_wait_deadline=child_wait_deadline,
            wait_timeout_seconds=wait_timeout_seconds,
        )
    submitted_parent = ComparisonReportRecord.model_validate(parent_payload)
    _validate_dispatch(
        report=report,
        context=context,
        application_name=application_name,
    )
    _validate_submitted_parent(
        report=report,
        context=context,
        parent=submitted_parent,
    )
    persistence = store or runtime_store(runtime)
    artifact_storage = artifacts or artifact_store()
    child_invoker = invoker or ModalChildInvoker()
    stage_plan: StagePlan = (
        STAGE12_SHADOW_REPORT_STAGES
        if context.production_function_call_id is not None
        else STAGE12_CANONICAL_REPORT_STAGES
    )
    progress = StageProgress()
    stage_span = partial(
        tracked_stage,
        stage_plan=stage_plan,
        progress=progress,
        runtime=runtime,
    )
    stage_scope = partial(stage_span, operation=True)
    with stage_span(Stage.STAGE12_COORDINATOR_CLAIM):
        parent, should_execute = _claim_parent(
            submitted=submitted_parent,
            coordinator_invocation_id=coordinator_invocation_id,
            store=persistence,
        )
    if not should_execute:
        return {
            "deduplicated": True,
            "evaluation_id": str(parent.evaluation_id),
            "status": parent.status.value,
        }
    descriptors: dict[str, SimulationArtifactDescriptor] = {}
    calls: dict[str, ChildCall] = {}
    simulations: dict[str, PlannedSimulationExecutionInput] = {}
    try:
        with stage_scope(Stage.STAGE12_COORDINATOR_PREPARATION):
            context = context.model_copy(
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
            function_name = context.simulation_callable
            output_plan, planned_simulations = _plan_simulations(
                report,
                output_plan_resolver=output_plan_resolver,
                stage_scope=stage_scope,
            )
        simulations = {
            simulation.role.value: simulation for simulation in planned_simulations
        }
        with stage_span(Stage.STAGE12_CHILD_STATE_CREATE):
            children = {
                simulation.role: persistence.create_or_resolve_simulation(
                    _child_record(
                        simulation=simulation,
                        context=context,
                        function_name=function_name,
                    )
                ).record
                for simulation in planned_simulations
            }
        for simulation in planned_simulations:
            child = children[simulation.role]
            if child.status is ComparisonRunLifecycleStatus.SUCCEEDED:
                descriptor = descriptor_from_record(child, simulation)
                retained = artifact_storage.read(descriptor.artifact.uri)
                _require_artifact_digest(
                    retained,
                    descriptor.artifact.content_sha256,
                    mismatch_message="retained child artifact digest mismatch",
                )
                descriptor = descriptor.model_copy(
                    update={
                        "artifact": descriptor.artifact.model_copy(
                            update={"size_bytes": len(retained)}
                        ),
                        "calculation_provenance": (
                            deserialize_calculation_provenance(retained)
                        ),
                    }
                )
                descriptors[simulation.role.value] = descriptor
                continue
            if (
                child.status is ComparisonRunLifecycleStatus.RUNNING
                and child.modal_invocation_id is not None
                and not child.modal_invocation_id.startswith("dispatch-pending-")
            ):
                calls[simulation.role.value] = child_invoker.restore(
                    child.modal_invocation_id
                )
                continue
            dispatching_at = datetime.now(UTC)
            dispatch_placeholder = f"dispatch-pending-{uuid4()}"
            persistence.replace_simulation(
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
                with stage_span(
                    Stage.STAGE12_CHILD_DISPATCH,
                    {"simulation_role": simulation.role.value},
                ):
                    child_observability_context = (
                        runtime.capture_context() if runtime is not None else None
                    )
                    call = child_invoker.spawn(
                        application_name=application_name,
                        function_name=function_name,
                        environment=context.modal_environment,
                        simulation=simulation.model_dump(mode="json"),
                        context=_child_context_payload(context),
                        observability_context=child_observability_context,
                    )
            except Exception as error:  # noqa: BLE001  # any dispatch failure is recorded on the child and stops its started peers
                detail = report_failure(
                    error,
                    runtime=runtime,
                    failure_scope="child_dispatch",
                    default_code="simulation_dispatch_failed",
                    attributes=_simulation_failure_attributes(simulation),
                    progress=progress,
                )
                _record_child_failure(
                    store=persistence,
                    simulation=simulation,
                    detail=detail,
                )
                _cancel_pending_simulations(
                    calls=calls,
                    simulations=simulations,
                    roles=set(calls),
                    store=persistence,
                    detail=_cancellation(
                        f"the {simulation.role.value} simulation failed"
                    ),
                )
                raise Stage12ExecutionError(detail) from None
            calls[simulation.role.value] = call
            persistence.attach_simulation_invocation(
                simulation.simulation_execution_id,
                expected_placeholder=dispatch_placeholder,
                modal_invocation_id=call.object_id,
                updated_at=datetime.now(UTC),
            )
        # Every required call has been started before the coordinator observes
        # baseline and reform together through nonblocking result probes.
        with stage_span(Stage.STAGE12_CHILD_WAIT):
            _observe_simulations(
                calls=calls,
                simulations=simulations,
                descriptors=descriptors,
                child_failure_detail=partial(
                    _child_failure_detail,
                    store=persistence,
                    runtime=runtime,
                    progress=progress,
                ),
                cancel_simulations=partial(
                    _cancel_pending_simulations,
                    calls=calls,
                    simulations=simulations,
                    store=persistence,
                ),
                runtime=runtime,
                progress=progress,
                wait_stage_name=stage_plan.name(Stage.STAGE12_CHILD_WAIT),
                sleep=sleep,
                monotonic=monotonic,
                deadline=child_wait_deadline,
                timeout_seconds=wait_timeout_seconds,
            )
        outputs = _read_simulation_outputs(
            report=report,
            output_plan=output_plan,
            descriptors=descriptors,
            artifact_storage=artifact_storage,
            stage_span=stage_span,
        )
        with stage_span(Stage.STAGE12_AGGREGATION):
            aggregate = outputs.aggregate(report, aggregator)
        with stage_span(Stage.STAGE12_AGGREGATE_ARTIFACT_WRITE):
            aggregate_artifact = artifact_storage.write_aggregate(
                prefix=context.artifact_prefix,
                payload=aggregate,
            )
        descriptor = AggregateReportArtifactDescriptor(
            evaluation_id=report.evaluation_id,
            artifact=aggregate_artifact,
            baseline_artifact_sha256=outputs.baseline.artifact.content_sha256,
            reform_artifact_sha256=outputs.reform.artifact.content_sha256,
            bundle=report.baseline.bundle,
        )
        completed = datetime.now(UTC)
        completed_parent = persistence.replace_report(
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
            context=context,
            store=persistence,
            artifacts=artifact_storage,
            invoker=child_invoker,
            runtime=runtime,
            stage_plan=stage_plan,
        )
        return descriptor.model_dump(mode="json")
    # Persist a redacted cause for any coordinator, child-call, aggregation, or
    # artifact failure before returning a stable exception to Modal.
    except Exception as error:  # noqa: BLE001  # every failure is persisted before a stable exception returns to Modal
        if isinstance(error, Stage12ExecutionError):
            # Child, dispatch and wait-timeout failures raise
            # Stage12ExecutionError only after reporting the original failure
            # and stopping their unfinished peers.
            detail = error.detail
        else:
            # Any other failure is reported here, once, before any database
            # write. If it occurred after a child started, such as a failed
            # placeholder write or invocation attach, every started,
            # unfinished child is then stopped so none keeps running for a
            # failed report.
            detail = report_failure(
                error,
                runtime=runtime,
                failure_scope="report_coordination",
                default_code="report_coordination_failed",
                attributes={"evaluation_id": str(report.evaluation_id)},
                progress=progress,
            )
            _cancel_pending_simulations(
                calls=calls,
                simulations=simulations,
                roles=set(calls) - set(descriptors),
                store=persistence,
                detail=_cancellation("the coordinator failed"),
            )
        record_failure_cause(runtime, error, detail.error_summary)
        failed_at = datetime.now(UTC)
        persist_failure_record(
            partial(
                persistence.replace_report,
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
            runtime=runtime,
            failure_scope="report_coordination",
        )
        msg = "Stage 12 report coordination failed"
        raise RuntimeError(msg) from None
