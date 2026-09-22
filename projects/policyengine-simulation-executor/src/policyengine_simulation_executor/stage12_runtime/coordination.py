"""Coordination of a Stage 12 report's baseline and reform simulations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactDescriptor,
    ComparisonReportRecord,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    ReportExecutionInput,
    ResultComparisonStatus,
    SimulationArtifactDescriptor,
    SimulationExecutionInput,
    Stage12InvocationContext,
)

from policyengine_simulation_executor.stage12_artifacts import (
    Stage12ArtifactStore,
    deserialize_calculation_provenance,
    deserialize_simulation_frames,
)

from .aggregation import build_aggregate_report, validate_aligned_outputs
from .comparison import compare_completed_report
from .dependencies import (
    ChildCall,
    ChildInvoker,
    ComparisonStore,
    ModalChildInvoker,
    artifact_store,
    runtime_store,
)
from .simulation import descriptor_from_record, simulation_input_sha256

SIMULATION_WAIT_TIMEOUT_SECONDS = 3_000


def _child_record(
    *,
    simulation: SimulationExecutionInput,
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
    simulations = (report.baseline, report.reform)
    descriptors: dict[str, SimulationArtifactDescriptor] = {}
    calls: dict[str, ChildCall] = {}
    try:
        children = {
            simulation.role: persistence.create_or_resolve_simulation(
                _child_record(
                    simulation=simulation,
                    context=context,
                    function_name=function_name,
                )
            ).record
            for simulation in simulations
        }
        for simulation in simulations:
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
                call = child_invoker.spawn(
                    application_name=application_name,
                    function_name=function_name,
                    environment=context.modal_environment,
                    simulation=simulation.model_dump(mode="json"),
                    context=context.model_dump(mode="json"),
                )
            except Exception as error:
                failed_at = datetime.now(UTC)
                latest = persistence.get_simulation(simulation.simulation_execution_id)
                if latest.status is not ComparisonRunLifecycleStatus.SUCCEEDED:
                    persistence.replace_simulation(
                        latest.model_copy(
                            update={
                                "status": ComparisonRunLifecycleStatus.FAILED,
                                "error_code": "simulation_dispatch_failed",
                                "error_summary": type(error).__name__,
                                "updated_at": failed_at,
                                "completed_at": failed_at,
                            }
                        )
                    )
                raise
            calls[simulation.role.value] = call
            persistence.attach_simulation_invocation(
                simulation.simulation_execution_id,
                expected_placeholder=dispatch_placeholder,
                modal_invocation_id=call.object_id,
                updated_at=datetime.now(UTC),
            )
        # Every required call has been started before the coordinator waits.
        for role, call in calls.items():
            try:
                descriptors[role] = SimulationArtifactDescriptor.model_validate(
                    call.get(timeout=SIMULATION_WAIT_TIMEOUT_SECONDS)
                )
            except Exception as error:
                simulation = next(
                    item for item in simulations if item.role.value == role
                )
                child = persistence.get_simulation(simulation.simulation_execution_id)
                if child.status is not ComparisonRunLifecycleStatus.SUCCEEDED:
                    failed_at = datetime.now(UTC)
                    persistence.replace_simulation(
                        child.model_copy(
                            update={
                                "status": ComparisonRunLifecycleStatus.FAILED,
                                "error_code": "simulation_invocation_failed",
                                "error_summary": type(error).__name__,
                                "updated_at": failed_at,
                                "completed_at": failed_at,
                            }
                        )
                    )
                raise
        baseline = descriptors["baseline"]
        reform = descriptors["reform"]
        validate_aligned_outputs(report, baseline, reform)
        baseline_payload = artifact_storage.read(baseline.artifact.uri)
        reform_payload = artifact_storage.read(reform.artifact.uri)
        if sha256(baseline_payload).hexdigest() != baseline.artifact.content_sha256:
            raise ValueError("baseline artifact digest mismatch")
        if sha256(reform_payload).hexdigest() != reform.artifact.content_sha256:
            raise ValueError("reform artifact digest mismatch")
        aggregate = aggregator(
            report=report,
            baseline_frames=deserialize_simulation_frames(baseline_payload),
            reform_frames=deserialize_simulation_frames(reform_payload),
            baseline_descriptor=baseline,
            reform_descriptor=reform,
        )
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
        )
        return descriptor.model_dump(mode="json")
    # Persist any coordinator, child-call, aggregation, or artifact failure
    # before returning a stable exception to Modal.
    except Exception as error:  # noqa: BLE001
        failed_at = datetime.now(UTC)
        persistence.replace_report(
            parent.model_copy(
                update={
                    "status": ComparisonRunLifecycleStatus.FAILED,
                    "aggregation_status": ComparisonRunAggregationStatus.FAILED,
                    "error_code": "report_coordination_failed",
                    "error_summary": type(error).__name__,
                    "updated_at": failed_at,
                    "completed_at": failed_at,
                }
            )
        )
        raise RuntimeError("Stage 12 report coordination failed") from None
