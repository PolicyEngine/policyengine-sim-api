"""Coordination of a Stage 12 report's baseline and reform simulations."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
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
from .failures import (
    Stage12ExecutionError,
    Stage12FailureDetail,
    failure_detail_from_exception,
    failure_detail_from_record,
)
from .output_planning import (
    plan_simulation_input,
    resolve_report_output_plan,
    validate_output_frames,
)
from .simulation import descriptor_from_record, simulation_input_sha256
from .segmentation import should_segment_simulation

SIMULATION_WAIT_TIMEOUT_SECONDS = 3_000
SIMULATION_POLL_INITIAL_SECONDS = 0.25
SIMULATION_POLL_MAX_SECONDS = 1.0
SIMULATION_POLL_BACKOFF_FACTOR = 2.0

logger = logging.getLogger(__name__)


def _next_poll_interval(current: float) -> float:
    return min(
        current * SIMULATION_POLL_BACKOFF_FACTOR,
        SIMULATION_POLL_MAX_SECONDS,
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


def _cancel_pending_simulations(
    *,
    calls: dict[str, ChildCall],
    simulations: dict[str, PlannedSimulationExecutionInput],
    roles: set[str],
    store: ComparisonStore,
    failed_role: str,
) -> None:
    detail = Stage12FailureDetail(
        error_code="cancelled_after_peer_failure",
        error_summary=f"Cancelled after {failed_role} simulation failed.",
    )
    for role in sorted(roles):
        if role == failed_role:
            continue
        simulation = simulations[role]
        try:
            latest = _replace_running_simulation(
                store=store,
                simulation=simulation,
                status=ComparisonRunLifecycleStatus.INCOMPLETE,
                detail=detail,
            )
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Stage 12 peer state cancellation failed for %s (%s)",
                role,
                type(error).__name__,
            )
            continue
        if latest.status is not ComparisonRunLifecycleStatus.INCOMPLETE:
            continue
        if should_segment_simulation(simulation):
            # The simulation worker reads the persisted state and cancels the
            # segment handles it owns. Modal does not guarantee recursive
            # cancellation for calls created with spawn().
            continue
        try:
            calls[role].cancel()
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "Stage 12 peer cancellation failed for %s (%s)",
                role,
                type(error).__name__,
            )


def _child_failure_detail(
    *,
    error: BaseException,
    simulation: PlannedSimulationExecutionInput,
    store: ComparisonStore,
    runtime: ObservabilityRuntime | None,
) -> Stage12FailureDetail:
    child = store.get_simulation(simulation.simulation_execution_id)
    if (
        child.status is ComparisonRunLifecycleStatus.FAILED
        and child.error_code is not None
    ):
        return failure_detail_from_record(
            error_code=child.error_code,
            error_summary=child.error_summary,
        )
    detail = failure_detail_from_exception(
        error,
        runtime=runtime,
        scope="stage12_simulation_invocation",
        default_code="simulation_invocation_failed",
        context={
            "evaluation_id": str(simulation.evaluation_id),
            "simulation_role": simulation.role.value,
        },
    )
    _replace_running_simulation(
        store=store,
        simulation=simulation,
        status=ComparisonRunLifecycleStatus.FAILED,
        detail=detail,
    )
    return detail


def _observe_simulations(
    *,
    calls: dict[str, ChildCall],
    simulations: dict[str, PlannedSimulationExecutionInput],
    descriptors: dict[str, SimulationArtifactDescriptor],
    store: ComparisonStore,
    runtime: ObservabilityRuntime | None,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    timeout_seconds: float,
) -> None:
    pending = set(calls)
    deadline = monotonic() + timeout_seconds
    poll_interval = SIMULATION_POLL_INITIAL_SECONDS

    while pending:
        progress_made = False
        for role in sorted(pending):
            call = calls[role]
            try:
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
                )
                _cancel_pending_simulations(
                    calls=calls,
                    simulations=simulations,
                    roles=pending,
                    store=store,
                    failed_role=role,
                )
                raise Stage12ExecutionError(detail) from None
            descriptors[role] = descriptor
            pending.remove(role)
            progress_made = True

        if not pending:
            return
        if monotonic() >= deadline:
            detail = Stage12FailureDetail(
                error_code="simulation_wait_timeout",
                error_summary=(
                    "Stage 12 simulations did not finish within "
                    f"{timeout_seconds:g} seconds."
                ),
            )
            for role in sorted(pending):
                _replace_running_simulation(
                    store=store,
                    simulation=simulations[role],
                    status=ComparisonRunLifecycleStatus.FAILED,
                    detail=detail,
                )
                if not should_segment_simulation(simulations[role]):
                    try:
                        calls[role].cancel()
                    except Exception as error:  # noqa: BLE001
                        logger.warning(
                            "Stage 12 timeout cancellation failed for %s (%s)",
                            role,
                            type(error).__name__,
                        )
            raise Stage12ExecutionError(detail)
        sleep(poll_interval)
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
    output_plan_resolver: Callable[
        [ReportExecutionInput], Stage12OutputPlan
    ] = resolve_report_output_plan,
    runtime: ObservabilityRuntime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    wait_timeout_seconds: float = SIMULATION_WAIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
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
    persistence = store or runtime_store()
    artifact_storage = artifacts or artifact_store()
    child_invoker = invoker or ModalChildInvoker()
    stage_plan: StagePlan = (
        STAGE12_SHADOW_REPORT_STAGES
        if context.production_function_call_id is not None
        else STAGE12_CANONICAL_REPORT_STAGES
    )
    claim_span = (
        runtime.span(stage_plan.name(Stage.STAGE12_COORDINATOR_CLAIM))
        if runtime is not None
        else nullcontext()
    )
    with claim_span:
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
    descriptors: dict[str, SimulationArtifactDescriptor] = {}
    calls: dict[str, ChildCall] = {}
    try:
        output_plan = output_plan_resolver(report)
        planned_simulations = (
            plan_simulation_input(report.baseline, output_plan),
            plan_simulation_input(report.reform, output_plan),
        )
        simulations = {
            simulation.role.value: simulation for simulation in planned_simulations
        }
        child_state_span = (
            runtime.span(stage_plan.name(Stage.STAGE12_CHILD_STATE_CREATE))
            if runtime is not None
            else nullcontext()
        )
        with child_state_span:
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
                dispatch_span = (
                    runtime.span(
                        stage_plan.name(Stage.STAGE12_CHILD_DISPATCH),
                        attributes={"simulation_role": simulation.role.value},
                    )
                    if runtime is not None
                    else nullcontext()
                )
                with dispatch_span:
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
                detail = failure_detail_from_exception(
                    error,
                    runtime=runtime,
                    scope="stage12_simulation_dispatch",
                    default_code="simulation_dispatch_failed",
                    context={
                        "evaluation_id": str(report.evaluation_id),
                        "simulation_role": simulation.role.value,
                    },
                )
                _replace_running_simulation(
                    store=persistence,
                    simulation=simulation,
                    status=ComparisonRunLifecycleStatus.FAILED,
                    detail=detail,
                )
                _cancel_pending_simulations(
                    calls=calls,
                    simulations=simulations,
                    roles=set(calls),
                    store=persistence,
                    failed_role=simulation.role.value,
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
        wait_span = (
            runtime.span(stage_plan.name(Stage.STAGE12_CHILD_WAIT))
            if runtime is not None
            else nullcontext()
        )
        with wait_span:
            _observe_simulations(
                calls=calls,
                simulations=simulations,
                descriptors=descriptors,
                store=persistence,
                runtime=runtime,
                sleep=sleep,
                monotonic=monotonic,
                timeout_seconds=wait_timeout_seconds,
            )
        baseline = descriptors["baseline"]
        reform = descriptors["reform"]
        validate_aligned_outputs(report, output_plan, baseline, reform)
        artifact_read_span = (
            runtime.span(stage_plan.name(Stage.STAGE12_ARTIFACT_READ))
            if runtime is not None
            else nullcontext()
        )
        with artifact_read_span:
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
        )
        validate_output_frames(baseline_frames, output_plan)
        validate_output_frames(reform_frames, output_plan)
        aggregation_span = (
            runtime.span(stage_plan.name(Stage.STAGE12_AGGREGATION))
            if runtime is not None
            else nullcontext()
        )
        with aggregation_span:
            aggregate = aggregator(
                report=report,
                baseline_frames=baseline_frames,
                reform_frames=reform_frames,
                baseline_descriptor=baseline,
                reform_descriptor=reform,
                uk_local_authority_metadata=uk_local_authority_metadata,
            )
        aggregate_write_span = (
            runtime.span(stage_plan.name(Stage.STAGE12_AGGREGATE_ARTIFACT_WRITE))
            if runtime is not None
            else nullcontext()
        )
        with aggregate_write_span:
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
    # Persist any coordinator, child-call, aggregation, or artifact failure
    # before returning a stable exception to Modal.
    except Exception as error:  # noqa: BLE001
        failed_at = datetime.now(UTC)
        detail = failure_detail_from_exception(
            error,
            runtime=runtime,
            scope="stage12_report_coordination",
            default_code="report_coordination_failed",
            context={"evaluation_id": str(report.evaluation_id)},
        )
        persistence.replace_report(
            parent.model_copy(
                update={
                    "status": ComparisonRunLifecycleStatus.FAILED,
                    "aggregation_status": ComparisonRunAggregationStatus.FAILED,
                    "error_code": detail.error_code,
                    "error_summary": detail.error_summary,
                    "updated_at": failed_at,
                    "completed_at": failed_at,
                }
            )
        )
        raise RuntimeError("Stage 12 report coordination failed") from None
