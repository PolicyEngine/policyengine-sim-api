"""Check a dispatched report against its parent record, then claim the parent."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ReportExecutionInput,
    ResultComparisonStatus,
    Stage12InvocationContext,
)

if TYPE_CHECKING:
    from .dependencies import ComparisonStore


def validate_dispatch_context(
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


def validate_submitted_parent(
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


def claim_parent(
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
