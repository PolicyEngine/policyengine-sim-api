"""A report run's status, stage and error, from its rows.

The economy report reads and the budget-window scheduler and reads all
describe a report run the same way: failed as soon as a simulation run or its
comparison failed, succeeded once its comparison succeeded, and otherwise
pending until a call starts and running after.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_contract.job_progress import JobStage
from policyengine_simulation_persistence import RunStatus

from policyengine_simulation_entry.economy_schemas import (
    ReportStatus,
    RunErrorResponse,
)

if TYPE_CHECKING:
    from policyengine_simulation_persistence import (
        ReportRunRecord,
        SimulationRunRecord,
    )


def run_error_response(
    code: str | None, message: str | None
) -> RunErrorResponse | None:
    """Describe a run's error, or None when it has none."""
    return None if code is None else RunErrorResponse(code=code, message=message)


def report_run_progress(
    report_run: ReportRunRecord,
    runs: tuple[SimulationRunRecord, SimulationRunRecord],
) -> tuple[ReportStatus, JobStage | None, RunErrorResponse | None]:
    """Return a report run's status, its stage while it runs, and its error.

    A failed simulation run fails the report run at once, before the safety
    check fails its comparison step.
    """
    failed = next((run for run in runs if run.status is RunStatus.FAILED), None)
    if failed is not None:
        return (
            ReportStatus.FAILED,
            None,
            run_error_response(failed.error_code, failed.error_message),
        )
    comparison = report_run.comparison_status
    if comparison is RunStatus.FAILED:
        return (
            ReportStatus.FAILED,
            None,
            run_error_response(
                report_run.comparison_error_code, report_run.comparison_error_message
            ),
        )
    if comparison is RunStatus.SUCCEEDED:
        return ReportStatus.SUCCEEDED, None, None
    if all(run.status is RunStatus.SUCCEEDED for run in runs):
        return ReportStatus.RUNNING, JobStage.OUTPUTS, None
    if all(run.status is RunStatus.PENDING for run in runs):
        return ReportStatus.PENDING, JobStage.QUEUED, None
    return ReportStatus.RUNNING, JobStage.SIMULATING, None
