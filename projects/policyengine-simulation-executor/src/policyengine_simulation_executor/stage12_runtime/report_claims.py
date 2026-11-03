"""Claiming a call's Stage 12 report, and the report run its call names.

A comparison run claims only its Stage 12 report, as before. A call that names
a report run claims that run first, under its Modal call ID, then its Stage 12
report. Each claim either lets the call run the report or gives the summary
the call returns instead.
"""

from __future__ import annotations

from functools import partial
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactPayload,
    ComparisonRunLifecycleStatus,
)
from policyengine_stage12_persistence import RunStatus

from .failure_recording import persist_failure_record
from .failures import Stage12FailureDetail
from .parent_claim import claim_parent

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_execution import (
        ComparisonReportRecord,
    )

    from .report_coordination import ReportCoordination
    from .report_run_recording import ReportRunRecorder


def claim_report(
    coordination: ReportCoordination,
    *,
    submitted: ComparisonReportRecord,
) -> tuple[ComparisonReportRecord | None, dict[str, Any] | None]:
    """Return the Stage 12 report to run, or the summary to return instead.

    A report run another call holds, or one already finished, returns its
    status. A call Modal restarted after its Stage 12 report succeeded, but
    before its report run did, completes the report run from the stored
    aggregate and returns that it succeeded. Once this call holds the report
    run, any error before the report starts fails the run, then propagates.
    """
    report_run = coordination.report_run
    if report_run is None:
        parent, should_execute = claim_parent(
            submitted=submitted,
            coordinator_invocation_id=coordination.invocation.invocation_id,
            store=coordination.persistence,
        )
        return (parent, None) if should_execute else (None, _stage12_summary(parent))
    claimed = report_run.claim()
    if not claimed.changed:
        return None, _report_run_summary(report_run, claimed.status)
    try:
        return _claim_held_report(coordination, report_run, submitted=submitted)
    except Exception as error:
        persist_failure_record(
            partial(
                report_run.fail,
                Stage12FailureDetail(
                    error_code="report_claim_failed",
                    error_summary=(
                        f"Claiming the Stage 12 report failed ({type(error).__name__})."
                    ),
                ),
            ),
            runtime=coordination.runtime,
            failure_scope="report_claim",
        )
        raise


def _claim_held_report(
    coordination: ReportCoordination,
    report_run: ReportRunRecorder,
    *,
    submitted: ComparisonReportRecord,
) -> tuple[ComparisonReportRecord | None, dict[str, Any] | None]:
    """Claim the Stage 12 report of a report run this call holds."""
    invocation_id = coordination.invocation.invocation_id
    parent, should_execute = claim_parent(
        submitted=submitted,
        coordinator_invocation_id=invocation_id,
        store=coordination.persistence,
        resume_own_claim=True,
    )
    if should_execute:
        return parent, None
    if (
        parent.status is ComparisonRunLifecycleStatus.SUCCEEDED
        and parent.coordinator_invocation_id == invocation_id
    ):
        coordination.complete_report_run(_stored_aggregate(coordination, parent))
        return None, _report_run_summary(report_run, RunStatus.SUCCEEDED)
    report_run.fail(
        Stage12FailureDetail(
            error_code="report_already_claimed",
            error_summary=(
                f"This report's Stage 12 record is {parent.status.value} "
                "under another call."
            ),
        )
    )
    return None, _stage12_summary(parent)


def _stored_aggregate(
    coordination: ReportCoordination, parent: ComparisonReportRecord
) -> dict[str, Any]:
    """Read and digest-check the aggregate a succeeded Stage 12 report names."""
    if parent.aggregate_output_uri is None or parent.aggregate_output_sha256 is None:
        msg = "a succeeded Stage 12 report names no aggregate"
        raise ValueError(msg)
    payload = coordination.artifact_storage.read(parent.aggregate_output_uri)
    if sha256(payload).hexdigest() != parent.aggregate_output_sha256:
        msg = "stored aggregate artifact digest mismatch"
        raise ValueError(msg)
    return AggregateReportArtifactPayload.model_validate_json(payload).model_dump(
        mode="json"
    )


def _report_run_summary(
    report_run: ReportRunRecorder, status: RunStatus
) -> dict[str, Any]:
    return {
        "deduplicated": True,
        "report_run_id": str(report_run.run_ids.report_run_id),
        "status": status.value,
    }


def _stage12_summary(parent: ComparisonReportRecord) -> dict[str, Any]:
    return {
        "deduplicated": True,
        "evaluation_id": str(parent.evaluation_id),
        "status": parent.status.value,
    }
