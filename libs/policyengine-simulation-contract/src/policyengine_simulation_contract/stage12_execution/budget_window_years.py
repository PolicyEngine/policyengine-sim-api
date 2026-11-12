"""The coordinator input of each year of a budget window, derived from the first.

A budget window's years compare the same policies on the same dataset,
region, options and bundle; only the year and the runs each year records on
differ. The simulation service builds the first year's coordinator input,
and the input of every other year, whether the service or an earlier year's
coordinator starts that year, is derived from it here, so all years are
calculated alike.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_contract.stage12_execution.comparison_runs import (
    Stage12InvocationContext,
)
from policyengine_simulation_contract.stage12_execution.report_inputs import (
    ReportExecutionInput,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_contract.stage12_execution.base import (
        SimulationExecutionInput,
    )
    from policyengine_simulation_contract.stage12_execution.comparison_runs import (
        ReportRunIds,
    )


def report_run_artifact_prefix(
    environment: str, created_at: datetime, run_id: UUID
) -> str:
    """Return where a run's Stage 12 artifacts go, by environment, month and run.

    The prefix is ``stage-12-runs/<environment>/<YYYY>/<MM>/<run_id>``. Raises
    ValueError for an environment that could leave the prefix, or a
    ``created_at`` without a time zone.
    """
    if not environment or "/" in environment or ".." in environment:
        msg = "invalid Stage 12 artifact environment"
        raise ValueError(msg)
    if created_at.tzinfo is None:
        msg = "Stage 12 artifact timestamp must include a timezone"
        raise ValueError(msg)
    return f"stage-12-runs/{environment}/{created_at:%Y}/{created_at:%m}/{run_id}"


def year_calculation(
    report: ReportExecutionInput,
    context: Stage12InvocationContext,
    *,
    year: int,
    run_ids: ReportRunIds,
    created_at: datetime,
) -> tuple[ReportExecutionInput, Stage12InvocationContext]:
    """Return the coordinator input and context of ``year``, recorded on ``run_ids``.

    ``report`` and ``context`` are another year's: the same window's first
    year, or the year whose coordinator starts this one. The year, the three
    runs, the artifact prefix and the call's creation time change; the
    retention period stays as long as the other year's. Every other field is
    kept, and the result is validated as any coordinator input is, so a
    year outside the contract's range raises ValueError. A context naming no
    report run raises ValueError: only a report run's coordinator records a
    budget window's year.
    """
    if context.report_run is None:
        msg = "a call that names no report run starts no budget-window year"
        raise ValueError(msg)
    report_run_id = run_ids.report_run_id
    year_report = ReportExecutionInput.model_validate(
        {
            **report.model_dump(mode="json"),
            "evaluation_id": str(report_run_id),
            "baseline": _year_side(
                report.baseline,
                year=year,
                report_run_id=report_run_id,
                simulation_run_id=run_ids.baseline_simulation_run_id,
            ),
            "reform": _year_side(
                report.reform,
                year=year,
                report_run_id=report_run_id,
                simulation_run_id=run_ids.reform_simulation_run_id,
            ),
        }
    )
    retention = context.retention_expires_at - context.created_at
    year_context = Stage12InvocationContext.model_validate(
        {
            **context.model_dump(mode="json"),
            "report_run": run_ids.model_dump(mode="json"),
            "artifact_prefix": report_run_artifact_prefix(
                context.environment, created_at, report_run_id
            ),
            "created_at": created_at.isoformat(),
            "retention_expires_at": (created_at + retention).isoformat(),
        }
    )
    return year_report, year_context


def _year_side(
    side: SimulationExecutionInput,
    *,
    year: int,
    report_run_id: UUID,
    simulation_run_id: UUID,
) -> dict[str, object]:
    """Return one side's input for the year, recorded on its simulation run."""
    return {
        **side.model_dump(mode="json"),
        "year": year,
        "evaluation_id": str(report_run_id),
        "simulation_execution_id": str(simulation_run_id),
    }
