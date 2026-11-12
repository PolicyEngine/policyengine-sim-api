"""Claiming a comparison-path call's Stage 12 report.

A comparison run claims its Stage 12 report, its evaluation record. The
claim either lets the call run the report or gives the summary the call
returns instead. A detailed-save call claims its report run instead
(``report_run_coordination``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .parent_claim import claim_parent

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_execution import (
        ComparisonReportRecord,
    )

    from .report_coordination import ReportCoordination


def claim_report(
    coordination: ReportCoordination,
    *,
    submitted: ComparisonReportRecord,
) -> tuple[ComparisonReportRecord | None, dict[str, Any] | None]:
    """Return the Stage 12 report to run, or the summary to return instead.

    A report another call holds, or one already finished, returns its
    status.
    """
    parent, should_execute = claim_parent(
        submitted=submitted,
        coordinator_invocation_id=coordination.invocation.invocation_id,
        store=coordination.persistence,
    )
    return (parent, None) if should_execute else (None, _stage12_summary(parent))


def _stage12_summary(parent: ComparisonReportRecord) -> dict[str, Any]:
    return {
        "deduplicated": True,
        "evaluation_id": str(parent.evaluation_id),
        "status": parent.status.value,
    }
