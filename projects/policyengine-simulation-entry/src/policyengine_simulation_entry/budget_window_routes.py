"""The v2 budget-window report routes.

- ``POST /v2/budget-window-reports``: create a budget-window report and its
  first run, and start its first years (``BudgetWindowReports.submit``); 201.
- ``GET /v2/budget-window-reports/{report_id}``: the report, its newest run
  with each year's progress and, once every year has succeeded, its result
  in today's budget-window format, with totals summed when read.
- ``POST /v2/budget-window-reports/{report_id}/runs``: calculate it again;
  201. A ``system`` run returns the report's newest run when it has one.
- ``GET /v2/budget-window-reports/{report_id}/runs``: its runs, newest first.

Submissions need ``simulation:submit`` and reads ``simulation:read``. Without
the simulation database, or the Modal environment sim starts calls in, every
route answers 503. The routes declare their error answers as the economy
report routes do (``economy_routes``), so the generated client parses them;
the report read also answers 500 when a succeeded run's stored figures
cannot be read back.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003  # FastAPI evaluates the handlers' path parameters

from fastapi import Depends, FastAPI, HTTPException, status
from policyengine_simulation_persistence import RunTrigger

from policyengine_simulation_entry.budget_window_reads import StoredWindowResultError
from policyengine_simulation_entry.budget_window_reports import (
    BudgetWindowReportSubmission,
)
from policyengine_simulation_entry.budget_window_schemas import (
    BudgetWindowReportResponse,
    BudgetWindowReportSubmissionRequest,
    BudgetWindowRunResponse,
    BudgetWindowRunsResponse,
)
from policyengine_simulation_entry.economy_report_types import (
    EconomyReportSubmission,
    SimulationChoice,
)
from policyengine_simulation_entry.economy_routes import (
    REFUSED,
    RESULT_UNREADABLE,
    UNAVAILABLE,
    answer_route,
)
from policyengine_simulation_entry.economy_schemas import (
    EconomyErrorResponse,
    EconomyReportRunRequest,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from policyengine_simulation_entry.auth import AuthorizationDependency
    from policyengine_simulation_entry.budget_window_reads import (
        BudgetWindowReportReads,
    )
    from policyengine_simulation_entry.budget_window_reports import (
        BudgetWindowReports,
    )

logger = logging.getLogger(__name__)

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {
        "model": EconomyErrorResponse,
        "description": "No such budget-window report",
    },
}


@dataclass(frozen=True)
class BudgetWindowRouteServices:
    """What the routes run on: the budget-window path and its status reads."""

    reports: BudgetWindowReports
    reads: BudgetWindowReportReads


async def _answer[ResultT](call: Callable[[], Awaitable[ResultT]]) -> ResultT:
    """Answer as the economy routes do, and 500 for unreadable stored figures."""
    try:
        return await answer_route(call)
    except StoredWindowResultError as error:
        logger.error(  # noqa: TRY400  # result failure logs carry only the run ID and the error type; a traceback could expose stored result data
            "budget_window_result_unreadable",
            extra={
                "budget_window_run_id": str(error.budget_window_run_id),
                "error_type": type(error.__cause__).__name__,
            },
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "The report's stored result cannot be read back.",
        ) from None


def _configured(
    services: BudgetWindowRouteServices | None,
) -> BudgetWindowRouteServices:
    if services is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Budget-window reports are not configured on this deployment.",
        )
    return services


def _submission(
    body: BudgetWindowReportSubmissionRequest,
) -> BudgetWindowReportSubmission:
    return BudgetWindowReportSubmission(
        report=EconomyReportSubmission(
            country=body.country,
            policyengine_version=body.policyengine_version,
            year=body.start_year,
            region_code=body.region_code,
            dataset_name=body.dataset_name,
            baseline=SimulationChoice(**body.baseline.model_dump()),
            reform=SimulationChoice(**body.reform.model_dump()),
            options=body.options,
        ),
        window_size=body.window_size,
    )


def register_budget_window_routes(
    app: FastAPI,
    *,
    services: BudgetWindowRouteServices | None,
    submitter: AuthorizationDependency,
    reader: AuthorizationDependency,
) -> None:
    """Add the v2 budget-window report routes."""

    @app.post(
        "/v2/budget-window-reports",
        status_code=status.HTTP_201_CREATED,
        response_model=BudgetWindowReportResponse,
        operation_id="submit_budget_window_report",
        responses={**REFUSED, **UNAVAILABLE},
        dependencies=[Depends(submitter)],
    )
    async def submit_budget_window_report(
        body: BudgetWindowReportSubmissionRequest,
    ) -> BudgetWindowReportResponse:
        windows = _configured(services)

        async def submit() -> BudgetWindowReportResponse:
            report, _ = await windows.reports.submit(_submission(body))
            return await asyncio.to_thread(windows.reads.report, report)

        return await _answer(submit)

    @app.get(
        "/v2/budget-window-reports/{report_id}",
        response_model=BudgetWindowReportResponse,
        operation_id="get_budget_window_report",
        responses={**NOT_FOUND, **REFUSED, **RESULT_UNREADABLE, **UNAVAILABLE},
        dependencies=[Depends(reader)],
    )
    async def get_budget_window_report(report_id: UUID) -> BudgetWindowReportResponse:
        windows = _configured(services)
        return await _answer(
            lambda: asyncio.to_thread(windows.reads.report_by_id, report_id)
        )

    @app.post(
        "/v2/budget-window-reports/{report_id}/runs",
        status_code=status.HTTP_201_CREATED,
        response_model=BudgetWindowRunResponse,
        operation_id="run_budget_window_report",
        responses={**NOT_FOUND, **REFUSED, **UNAVAILABLE},
        dependencies=[Depends(submitter)],
    )
    async def run_budget_window_report(
        report_id: UUID, body: EconomyReportRunRequest
    ) -> BudgetWindowRunResponse:
        windows = _configured(services)

        async def run() -> BudgetWindowRunResponse:
            budget_window_run = await windows.reports.run_report(
                report_id,
                policyengine_version=body.policyengine_version,
                trigger=RunTrigger(body.trigger),
                force=body.force,
            )
            return await asyncio.to_thread(
                lambda: windows.reads.run(
                    budget_window_run,
                    windows.reads.report_record(budget_window_run.report_id),
                )
            )

        return await _answer(run)

    @app.get(
        "/v2/budget-window-reports/{report_id}/runs",
        response_model=BudgetWindowRunsResponse,
        operation_id="list_budget_window_report_runs",
        responses={**NOT_FOUND, **REFUSED, **UNAVAILABLE},
        dependencies=[Depends(reader)],
    )
    async def list_budget_window_report_runs(
        report_id: UUID,
    ) -> BudgetWindowRunsResponse:
        windows = _configured(services)
        return await _answer(
            lambda: asyncio.to_thread(windows.reads.report_runs, report_id)
        )
