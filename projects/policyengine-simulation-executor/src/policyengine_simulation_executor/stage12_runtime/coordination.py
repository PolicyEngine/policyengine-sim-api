"""Coordination of a Stage 12 report's baseline and reform simulations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
import logging
import time
from typing import Any
from uuid import uuid4

from policyengine_observability import ObservabilityRuntime
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
)
from .output_planning import (
    plan_simulation_input,
    resolve_report_output_plan,
    validate_output_frames,
)
from .simulation import descriptor_from_record, simulation_input_sha256
from .segmentation import should_segment_simulation

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
        except Exception as error:  # noqa: BLE001
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
        except Exception as error:  # noqa: BLE001
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
    except Exception as state_error:  # noqa: BLE001
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
    except Exception as state_error:  # noqa: BLE001
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


def _observe_simulations(
    *,
    calls: dict[str, ChildCall],
    simulations: dict[str, PlannedSimulationExecutionInput],
    descriptors: dict[str, SimulationArtifactDescriptor],
    store: ComparisonStore,
    runtime: ObservabilityRuntime | None,
    progress: StageProgress,
    wait_stage_name: str,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    deadline: float,
    timeout_seconds: float,
) -> None:
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
            except Exception as error:  # noqa: BLE001
                detail = _child_failure_detail(
                    error=error,
                    simulation=simulations[role],
                    store=store,
                    runtime=runtime,
                    progress=progress,
                )
                _cancel_pending_simulations(
                    calls=calls,
                    simulations=simulations,
                    roles=pending - {role},
                    store=store,
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
            _cancel_pending_simulations(
                calls=calls,
                simulations=simulations,
                roles=pending,
                store=store,
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
            raise ValueError(f"submitted parent {field} does not match invocation")
    if parent.status is not ComparisonRunLifecycleStatus.PENDING:
        raise ValueError("submitted parent must be pending")
    if parent.aggregation_status is not ComparisonRunAggregationStatus.NOT_STARTED:
        raise ValueError("submitted parent aggregation must not be started")
    if parent.coordinator_invocation_id is not None:
        raise ValueError("submitted parent must not contain an invocation identifier")
    production_job_id = context.production_function_call_id
    if production_job_id is None:
        if parent.production_identity != f"direct:{report.evaluation_id}":
            raise ValueError("direct parent identity does not match its report")
        if parent.incumbent_execution_id is not None:
            raise ValueError("direct parent must not name a production execution")
        if parent.comparison_status is not ResultComparisonStatus.NOT_REQUESTED:
            raise ValueError("direct parent must not request result comparison")
    elif (
        parent.production_identity != production_job_id
        or parent.incumbent_execution_id != production_job_id
    ):
        raise ValueError("automatic parent does not match the production execution")


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
        raise ValueError(
            "existing comparison identity has a different report identifier"
        )
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


def coordinate_report(
    payload: object,
    context_payload: object,
    parent_payload: object,
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
    # Open the shared child-wait window before any setup work.
    child_wait_deadline = monotonic() + wait_timeout_seconds
    report = ReportExecutionInput.model_validate(payload)
    context = Stage12InvocationContext.model_validate(context_payload)
    submitted_parent = ComparisonReportRecord.model_validate(parent_payload)
    if application_name != context.modal_application:
        raise ValueError("report coordinator application differs from dispatch context")
    if report.baseline.bundle.policyengine_version != context.worker_version:
        raise ValueError("report worker version differs from dispatch context")
    if report.baseline.bundle.bundle_manifest_sha256 != context.bundle_manifest_sha256:
        raise ValueError("report bundle digest differs from dispatch context")
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
                    planned_inputs.append(
                        plan_simulation_input(simulation, output_plan)
                    )
            planned_simulations = tuple(planned_inputs)
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
                if sha256(retained).hexdigest() != descriptor.artifact.content_sha256:
                    raise ValueError("retained child artifact digest mismatch")
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
                        context=context.model_dump(mode="json"),
                        observability_context=child_observability_context,
                    )
            except Exception as error:
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
                store=persistence,
                runtime=runtime,
                progress=progress,
                wait_stage_name=stage_plan.name(Stage.STAGE12_CHILD_WAIT),
                sleep=sleep,
                monotonic=monotonic,
                deadline=child_wait_deadline,
                timeout_seconds=wait_timeout_seconds,
            )
        baseline = descriptors["baseline"]
        reform = descriptors["reform"]
        validate_aligned_outputs(report, output_plan, baseline, reform)
        with stage_span(Stage.STAGE12_ARTIFACT_READ):
            baseline_payload = artifact_storage.read(baseline.artifact.uri)
            reform_payload = artifact_storage.read(reform.artifact.uri)
        if sha256(baseline_payload).hexdigest() != baseline.artifact.content_sha256:
            raise ValueError("baseline artifact digest mismatch")
        if sha256(reform_payload).hexdigest() != reform.artifact.content_sha256:
            raise ValueError("reform artifact digest mismatch")
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
        with stage_span(Stage.STAGE12_AGGREGATION):
            aggregate = aggregator(
                report=report,
                baseline_frames=baseline_frames,
                reform_frames=reform_frames,
                baseline_descriptor=baseline,
                reform_descriptor=reform,
                uk_local_authority_metadata=uk_local_authority_metadata,
            )
        with stage_span(Stage.STAGE12_AGGREGATE_ARTIFACT_WRITE):
            aggregate_artifact = artifact_storage.write_aggregate(
                prefix=context.artifact_prefix,
                payload=aggregate,
            )
        descriptor = AggregateReportArtifactDescriptor(
            evaluation_id=report.evaluation_id,
            artifact=aggregate_artifact,
            baseline_artifact_sha256=baseline.artifact.content_sha256,
            reform_artifact_sha256=reform.artifact.content_sha256,
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
    except Exception as error:  # noqa: BLE001
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
        raise RuntimeError("Stage 12 report coordination failed") from None
