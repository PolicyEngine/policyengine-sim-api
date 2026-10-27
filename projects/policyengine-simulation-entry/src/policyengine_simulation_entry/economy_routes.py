"""The v2 economy report routes, and the status reads they answer with.

- ``POST /v2/economy-reports``: create a report and its first run, and start
  it (``EconomyReports.submit``); 201.
- ``GET /v2/economy-reports/{report_id}`` and
  ``GET /v2/economy-reports/by-legacy-id/{legacy_report_id}``: the report,
  its newest run and, once that run's comparison succeeded, its result,
  rebuilt from its rows in today's format (``SingleYearMacroOutput``).
- ``POST /v2/economy-reports/{report_id}/runs``: run it again; 201.
- ``GET /v2/economy-reports/{report_id}/runs``: its runs, newest first, each
  with its catalog version.
- ``GET /v2/simulations/{simulation_id}``: a simulation, so a caller can
  check that one exists before saving its ID.

Status reads only read. Submissions need ``simulation:submit`` and reads
``simulation:read``. Without the simulation database, or the Modal
environment sim starts calls in, every route answers 503.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID  # noqa: TC003  # FastAPI evaluates the handlers' path parameters

from fastapi import Depends, FastAPI, HTTPException, status
from policyengine_simulation_contract.job_progress import JobStage
from policyengine_simulation_persistence import ReportKind, RunStatus, RunTrigger
from sqlalchemy.exc import DBAPIError

from policyengine_simulation_entry.economy_report_types import (
    EconomyReportSubmission,
    EconomyRequestError,
    ReportDefinitions,
    ReportNotFoundError,
    SimulationChoice,
)
from policyengine_simulation_entry.economy_schemas import (
    ComparisonResponse,
    EconomyReportResponse,
    EconomyReportRunRequest,
    EconomyReportRunResponse,
    EconomyReportRunsResponse,
    EconomyReportSubmissionRequest,
    ReportStatus,
    RunErrorResponse,
    SimulationResponse,
    SimulationRunResponse,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
    from policyengine_simulation_persistence import (
        ReportRecord,
        ReportRunRecord,
        SimulationRecord,
        SimulationRunRecord,
    )

    from policyengine_simulation_entry.auth import AuthorizationDependency
    from policyengine_simulation_entry.economy_report_types import (
        EconomyCatalog,
        SimulationDefinitions,
    )
    from policyengine_simulation_entry.economy_reports import EconomyReports

logger = logging.getLogger(__name__)


class ResultReader(Protocol):
    """Rebuild a succeeded comparison's result from its rows."""

    def read_result(
        self, report_run_id: UUID, catalog_version_id: UUID, country: str
    ) -> SingleYearMacroOutput:
        """Return the result; raise LookupError when it cannot be rebuilt."""
        ...


class ReportReads(ReportDefinitions, Protocol):
    """``ReportStore``'s reads by v1 report number."""

    def get_by_legacy_report_id(self, legacy_report_id: int) -> ReportRecord | None:
        """Return the report migrated from this v1 report, or None."""
        ...


class RunReads(Protocol):
    """``RunStore``'s reads."""

    def get_simulation_run(self, run_id: UUID) -> SimulationRunRecord | None:
        """Return a simulation run, or None."""
        ...

    def report_runs(self, report_id: UUID) -> list[ReportRunRecord]:
        """Return a report's runs, newest first."""
        ...


def _error(code: str | None, message: str | None) -> RunErrorResponse | None:
    return None if code is None else RunErrorResponse(code=code, message=message)


def _simulation(record: SimulationRecord) -> SimulationResponse:
    return SimulationResponse(
        id=record.id,
        kind=record.kind.value,
        country=record.country,
        policy_id=record.policy_id,
        dynamic_id=record.dynamic_id,
        region_code=record.region_code,
        dataset_name=record.dataset_name,
        legacy_simulation_id=record.legacy_simulation_id,
        created_at=record.created_at,
    )


def _simulation_run(record: SimulationRunRecord) -> SimulationRunResponse:
    return SimulationRunResponse(
        id=record.id,
        simulation_id=record.simulation_id,
        status=record.status.value,
        attempt=record.attempt,
        trigger=record.trigger.value,
        error=_error(record.error_code, record.error_message),
        dispatched_at=record.dispatched_at,
        completed_at=record.completed_at,
        created_at=record.created_at,
    )


def _progress(
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
            _error(failed.error_code, failed.error_message),
        )
    comparison = report_run.comparison_status
    if comparison is RunStatus.FAILED:
        return (
            ReportStatus.FAILED,
            None,
            _error(
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


@dataclass
class EconomyReportReads:
    """Build report and report run responses from the stored rows."""

    simulations: SimulationDefinitions
    reports: ReportReads
    runs: RunReads
    catalog: EconomyCatalog
    results: ResultReader

    def report_run(self, report_run: ReportRunRecord) -> EconomyReportRunResponse:
        """Describe one report run with its simulation runs and comparison."""
        reform_run_id = (
            report_run.reform_simulation_run_id or report_run.baseline_simulation_run_id
        )
        baseline = self.runs.get_simulation_run(report_run.baseline_simulation_run_id)
        reform = self.runs.get_simulation_run(reform_run_id)
        if baseline is None or reform is None:  # pragma: no cover - foreign keys
            msg = f"report run {report_run.id} has no simulation run"
            raise LookupError(msg)
        version = self.catalog.catalog_version_by_id(
            report_run.tax_benefit_model_version_id
        )
        report_status, stage, error = _progress(report_run, (baseline, reform))
        return EconomyReportRunResponse(
            id=report_run.id,
            report_id=report_run.report_id,
            trigger=report_run.trigger.value,
            catalog_version_id=report_run.tax_benefit_model_version_id,
            policyengine_version=None
            if version is None
            else version.policyengine_version,
            year=report_run.year,
            status=report_status,
            stage=stage,
            error=error,
            baseline_run=_simulation_run(baseline),
            reform_run=_simulation_run(reform),
            comparison=ComparisonResponse(
                status=(report_run.comparison_status or RunStatus.PENDING).value,
                error=_error(
                    report_run.comparison_error_code,
                    report_run.comparison_error_message,
                ),
                dispatched_at=report_run.comparison_dispatched_at,
                completed_at=report_run.comparison_completed_at,
            ),
            created_at=report_run.created_at,
        )

    def report(self, report: ReportRecord) -> EconomyReportResponse:
        """Describe a report with its newest run and, once compared, its result."""
        simulations = [
            self.simulations.get(simulation_id)
            for simulation_id in (
                report.baseline_simulation_id,
                report.reform_simulation_id or report.baseline_simulation_id,
            )
        ]
        baseline, reform = simulations
        if baseline is None or reform is None:  # pragma: no cover - foreign keys
            msg = f"report {report.id} has no simulation"
            raise LookupError(msg)
        runs = self.runs.report_runs(report.id)
        latest = self.report_run(runs[0]) if runs else None
        result = None
        if latest is not None and latest.status is ReportStatus.SUCCEEDED:
            result = self.results.read_result(
                latest.id, latest.catalog_version_id, report.country
            )
        return EconomyReportResponse(
            id=report.id,
            country=report.country,
            year=report.year,
            options=report.options,
            legacy_report_id=report.legacy_report_id,
            created_at=report.created_at,
            baseline_simulation=_simulation(baseline),
            reform_simulation=_simulation(reform),
            status=ReportStatus.NOT_RUN if latest is None else latest.status,
            run=latest,
            result=result,
        )

    def report_by_id(self, report_id: UUID) -> EconomyReportResponse:
        """Describe a report; raise ReportNotFoundError for an unknown one."""
        return self.report(self._report(self.reports.get(report_id), report_id))

    def report_by_legacy_id(self, legacy_report_id: int) -> EconomyReportResponse:
        """Describe the report migrated from a v1 report number."""
        return self.report(
            self._report(
                self.reports.get_by_legacy_report_id(legacy_report_id),
                legacy_report_id,
            )
        )

    def report_runs(self, report_id: UUID) -> EconomyReportRunsResponse:
        """List a report's runs, newest first."""
        self._report(self.reports.get(report_id), report_id)
        return EconomyReportRunsResponse(
            runs=[self.report_run(run) for run in self.runs.report_runs(report_id)]
        )

    def simulation(self, simulation_id: UUID) -> SimulationResponse:
        """Describe a simulation; raise ReportNotFoundError for an unknown one."""
        record = self.simulations.get(simulation_id)
        if record is None:
            msg = f"no simulation {simulation_id}"
            raise ReportNotFoundError(msg)
        return _simulation(record)

    @staticmethod
    def _report(record: ReportRecord | None, identifier: object) -> ReportRecord:
        """Return an economy report; a household report is not one."""
        if record is None or record.kind is not ReportKind.ECONOMY:
            msg = f"no economy report {identifier}"
            raise ReportNotFoundError(msg)
        return record


@dataclass(frozen=True)
class EconomyRouteServices:
    """What the routes run on: the economy path and its status reads."""

    reports: EconomyReports
    reads: EconomyReportReads


async def _answer[ResultT](call: Callable[[], Awaitable[ResultT]]) -> ResultT:
    """Run a route's work, turning sim's refusals into HTTP errors."""
    try:
        return await call()
    except ReportNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None
    except EconomyRequestError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from None
    except DBAPIError as error:
        logger.warning(
            "economy_report_database_unavailable",
            extra={"error_type": type(error).__name__},
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The simulation database cannot be reached.",
        ) from None


def _configured(services: EconomyRouteServices | None) -> EconomyRouteServices:
    if services is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Economy reports are not configured on this deployment.",
        )
    return services


def register_economy_routes(
    app: FastAPI,
    *,
    services: EconomyRouteServices | None,
    submitter: AuthorizationDependency,
    reader: AuthorizationDependency,
) -> None:
    """Add the v2 economy report routes."""

    @app.post(
        "/v2/economy-reports",
        status_code=status.HTTP_201_CREATED,
        response_model=EconomyReportResponse,
        operation_id="submit_economy_report",
        dependencies=[Depends(submitter)],
    )
    async def submit_economy_report(
        body: EconomyReportSubmissionRequest,
    ) -> EconomyReportResponse:
        economy = _configured(services)

        async def submit() -> EconomyReportResponse:
            report, _ = await economy.reports.submit(
                EconomyReportSubmission(
                    country=body.country,
                    policyengine_version=body.policyengine_version,
                    year=body.year,
                    region_code=body.region_code,
                    dataset_name=body.dataset_name,
                    baseline=SimulationChoice(**body.baseline.model_dump()),
                    reform=SimulationChoice(**body.reform.model_dump()),
                    options=body.options,
                )
            )
            return await asyncio.to_thread(economy.reads.report, report)

        return await _answer(submit)

    @app.get(
        "/v2/economy-reports/by-legacy-id/{legacy_report_id}",
        response_model=EconomyReportResponse,
        operation_id="get_economy_report_by_legacy_id",
        dependencies=[Depends(reader)],
    )
    async def get_economy_report_by_legacy_id(
        legacy_report_id: int,
    ) -> EconomyReportResponse:
        economy = _configured(services)
        return await _answer(
            lambda: asyncio.to_thread(
                economy.reads.report_by_legacy_id, legacy_report_id
            )
        )

    @app.get(
        "/v2/economy-reports/{report_id}",
        response_model=EconomyReportResponse,
        operation_id="get_economy_report",
        dependencies=[Depends(reader)],
    )
    async def get_economy_report(report_id: UUID) -> EconomyReportResponse:
        economy = _configured(services)
        return await _answer(
            lambda: asyncio.to_thread(economy.reads.report_by_id, report_id)
        )

    @app.post(
        "/v2/economy-reports/{report_id}/runs",
        status_code=status.HTTP_201_CREATED,
        response_model=EconomyReportRunResponse,
        operation_id="run_economy_report",
        dependencies=[Depends(submitter)],
    )
    async def run_economy_report(
        report_id: UUID, body: EconomyReportRunRequest
    ) -> EconomyReportRunResponse:
        economy = _configured(services)

        async def run() -> EconomyReportRunResponse:
            report_run = await economy.reports.run_report(
                report_id,
                policyengine_version=body.policyengine_version,
                trigger=RunTrigger(body.trigger),
                force=body.force,
            )
            return await asyncio.to_thread(economy.reads.report_run, report_run)

        return await _answer(run)

    @app.get(
        "/v2/economy-reports/{report_id}/runs",
        response_model=EconomyReportRunsResponse,
        operation_id="list_economy_report_runs",
        dependencies=[Depends(reader)],
    )
    async def list_economy_report_runs(report_id: UUID) -> EconomyReportRunsResponse:
        economy = _configured(services)
        return await _answer(
            lambda: asyncio.to_thread(economy.reads.report_runs, report_id)
        )

    @app.get(
        "/v2/simulations/{simulation_id}",
        response_model=SimulationResponse,
        operation_id="get_simulation",
        dependencies=[Depends(reader)],
    )
    async def get_simulation(simulation_id: UUID) -> SimulationResponse:
        economy = _configured(services)
        return await _answer(
            lambda: asyncio.to_thread(economy.reads.simulation, simulation_id)
        )
