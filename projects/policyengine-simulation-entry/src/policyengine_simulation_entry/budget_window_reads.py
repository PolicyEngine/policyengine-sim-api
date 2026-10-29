"""Read budget-window reports: each year's progress, and the totals, summed on read.

A budget-window report reads with its newest run. A run reads with each year
of its window: the years it started have their report runs' status, stage and
error; the others are queued, or never run once the run has finished. A run
has failed as soon as one year has, before the scheduler records it, and has
succeeded once every year has. Once it has succeeded, its result is today's
budget-window format (``BudgetWindowReportResult``), built from each year's stored
budget row. The totals are summed in decimal arithmetic, so a ten-year sum
of large figures carries no binary rounding error, then returned as floats,
as the old gateway's budget windows are.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_contract.job_progress import JobStage
from policyengine_simulation_persistence import BudgetWindowStatus, ReportKind

from policyengine_simulation_entry.budget_window_reports import window_state
from policyengine_simulation_entry.budget_window_schemas import (
    BudgetWindowReportResponse,
    BudgetWindowReportResult,
    BudgetWindowRunResponse,
    BudgetWindowRunsResponse,
    BudgetWindowTotalImpact,
    BudgetWindowYearImpact,
    BudgetWindowYearResponse,
)
from policyengine_simulation_entry.economy_report_types import ReportNotFoundError
from policyengine_simulation_entry.economy_routes import simulation_response
from policyengine_simulation_entry.economy_schemas import (
    ReportStatus,
    RunErrorResponse,
)
from policyengine_simulation_entry.report_progress import (
    report_run_progress,
    run_error_response,
)

if TYPE_CHECKING:
    from uuid import UUID

    from policyengine_simulation_persistence import (
        BudgetWindowRunRecord,
        BudgetWindowYear,
        ReportRecord,
        YearBudget,
    )

    from policyengine_simulation_entry.economy_report_types import (
        EconomyCatalog,
        ReportDefinitions,
        SimulationDefinitions,
    )

_BUDGET_FIELDS = (
    "taxRevenueImpact",
    "federalTaxRevenueImpact",
    "stateTaxRevenueImpact",
    "benefitSpendingImpact",
    "budgetaryImpact",
)


class StoredWindowResultError(Exception):
    """A succeeded budget-window run's stored figures cannot be read back."""

    def __init__(self, budget_window_run_id: UUID, cause: LookupError) -> None:
        """Name the run and keep the reader's error as the cause."""
        super().__init__(f"budget-window run {budget_window_run_id}: {cause}")
        self.budget_window_run_id = budget_window_run_id


class BudgetWindowRunReads(Protocol):
    """``BudgetWindowRunStore``'s reads."""

    def latest_run(self, report_id: UUID) -> BudgetWindowRunRecord | None:
        """Return the report's newest run, or None."""
        ...

    def runs(self, report_id: UUID) -> list[BudgetWindowRunRecord]:
        """Return the report's runs, newest first."""
        ...

    def years(self, run_id: UUID) -> list[BudgetWindowYear]:
        """Return the years the run has started, with their runs."""
        ...

    def year_budgets(self, run_id: UUID) -> list[YearBudget]:
        """Return each stored year's budget figures, in year order."""
        ...


def _annual_impact(budget: YearBudget) -> BudgetWindowYearImpact:
    return BudgetWindowYearImpact(
        year=str(budget.year),
        taxRevenueImpact=budget.tax_revenue_impact,
        federalTaxRevenueImpact=(
            budget.tax_revenue_impact - budget.state_tax_revenue_impact
        ),
        stateTaxRevenueImpact=budget.state_tax_revenue_impact,
        benefitSpendingImpact=budget.benefit_spending_impact,
        budgetaryImpact=budget.budgetary_impact,
    )


def _totals(annual_impacts: list[BudgetWindowYearImpact]) -> BudgetWindowTotalImpact:
    """Sum the years' figures in decimal arithmetic, from each float's shortest form."""
    sums = {
        name: sum(
            (Decimal(str(getattr(impact, name))) for impact in annual_impacts),
            Decimal(0),
        )
        for name in _BUDGET_FIELDS
    }
    return BudgetWindowTotalImpact(
        taxRevenueImpact=float(sums["taxRevenueImpact"]),
        federalTaxRevenueImpact=float(sums["federalTaxRevenueImpact"]),
        stateTaxRevenueImpact=float(sums["stateTaxRevenueImpact"]),
        benefitSpendingImpact=float(sums["benefitSpendingImpact"]),
        budgetaryImpact=float(sums["budgetaryImpact"]),
    )


def budget_window_result(
    report: ReportRecord, budgets: list[YearBudget]
) -> BudgetWindowReportResult:
    """Return the window's result from each year's budget figures.

    Raise LookupError when a year of the window has no stored figures.
    """
    years = list(report.window_years)
    stored = [budget.year for budget in budgets]
    if stored != years:
        missing = sorted(set(years) - set(stored))
        msg = f"years {missing} of the window have no stored budget figures"
        raise LookupError(msg)
    annual_impacts = [_annual_impact(budget) for budget in budgets]
    return BudgetWindowReportResult(
        startYear=str(years[0]),
        endYear=str(years[-1]),
        windowSize=len(years),
        annualImpacts=annual_impacts,
        totals=_totals(annual_impacts),
    )


def _run_status(
    run: BudgetWindowRunRecord,
    report: ReportRecord,
    years: list[BudgetWindowYear],
) -> tuple[ReportStatus, RunErrorResponse | None]:
    if run.status is BudgetWindowStatus.FAILED:
        return ReportStatus.FAILED, run_error_response(
            run.error_code, run.error_message
        )
    state = window_state(report, years)
    if state.failed is not None:
        return ReportStatus.FAILED, run_error_response(
            state.failed.code.value, state.failed.message
        )
    if run.status is BudgetWindowStatus.SUCCEEDED or state.succeeded:
        return ReportStatus.SUCCEEDED, None
    calculating = any(
        report_run_progress(year.report_run, (year.baseline_run, year.reform_run))[0]
        is not ReportStatus.PENDING
        for year in years
    )
    return (ReportStatus.RUNNING if calculating else ReportStatus.PENDING), None


@dataclass
class BudgetWindowReportReads:
    """Build budget-window report and run responses from the stored rows."""

    simulations: SimulationDefinitions
    reports: ReportDefinitions
    windows: BudgetWindowRunReads
    catalog: EconomyCatalog

    def run(
        self, run: BudgetWindowRunRecord, report: ReportRecord
    ) -> BudgetWindowRunResponse:
        """Describe one budget-window run of ``report``, with each of its years."""
        years = self.windows.years(run.id)
        status, error = _run_status(run, report, years)
        started = {year.report_run.year: year for year in years}
        version = self.catalog.catalog_version_by_id(run.tax_benefit_model_version_id)
        year_responses = [
            self._year(window_year, started.get(window_year), finished=run.finished)
            for window_year in report.window_years
        ]
        succeeded = sum(
            year.status is ReportStatus.SUCCEEDED for year in year_responses
        )
        return BudgetWindowRunResponse(
            id=run.id,
            report_id=run.report_id,
            trigger=run.trigger.value,
            catalog_version_id=run.tax_benefit_model_version_id,
            policyengine_version=None
            if version is None
            else version.policyengine_version,
            force=run.force,
            max_concurrent_years=run.max_concurrent_years,
            status=status,
            error=error,
            progress=100 * succeeded // len(year_responses),
            years=year_responses,
            created_at=run.created_at,
            completed_at=run.completed_at,
        )

    @staticmethod
    def _year(
        window_year: int, year: BudgetWindowYear | None, *, finished: bool
    ) -> BudgetWindowYearResponse:
        if year is None:
            return BudgetWindowYearResponse(
                year=window_year,
                status=ReportStatus.NOT_RUN if finished else ReportStatus.PENDING,
                stage=None if finished else JobStage.QUEUED,
                error=None,
                report_run_id=None,
            )
        status, stage, error = report_run_progress(
            year.report_run, (year.baseline_run, year.reform_run)
        )
        return BudgetWindowYearResponse(
            year=window_year,
            status=status,
            stage=stage,
            error=error,
            report_run_id=year.report_run.id,
        )

    def report(self, report: ReportRecord) -> BudgetWindowReportResponse:
        """Describe a report with its newest run and, once it succeeded, its result.

        Raise StoredWindowResultError when a succeeded run's figures cannot
        be read.
        """
        baseline, reform = (
            self.simulations.get(simulation_id)
            for simulation_id in (
                report.baseline_simulation_id,
                report.reform_simulation_id or report.baseline_simulation_id,
            )
        )
        if baseline is None or reform is None:  # pragma: no cover - foreign keys
            msg = f"report {report.id} has no simulation"
            raise LookupError(msg)
        newest = self.windows.latest_run(report.id)
        run = None if newest is None else self.run(newest, report)
        result = None
        if (
            newest is not None
            and run is not None
            and run.status is ReportStatus.SUCCEEDED
        ):
            try:
                result = budget_window_result(
                    report, self.windows.year_budgets(newest.id)
                )
            except LookupError as error:
                raise StoredWindowResultError(newest.id, error) from error
        return BudgetWindowReportResponse(
            id=report.id,
            country=report.country,
            start_year=report.year,
            window_size=len(report.window_years),
            options=report.options,
            created_at=report.created_at,
            baseline_simulation=simulation_response(baseline),
            reform_simulation=simulation_response(reform),
            status=ReportStatus.NOT_RUN if run is None else run.status,
            run=run,
            result=result,
        )

    def report_by_id(self, report_id: UUID) -> BudgetWindowReportResponse:
        """Describe a report; raise ReportNotFoundError for one that is not a window."""
        return self.report(self.report_record(report_id))

    def report_runs(self, report_id: UUID) -> BudgetWindowRunsResponse:
        """List a budget-window report's runs, newest first."""
        report = self.report_record(report_id)
        return BudgetWindowRunsResponse(
            runs=[self.run(run, report) for run in self.windows.runs(report_id)]
        )

    def report_record(self, report_id: UUID) -> ReportRecord:
        """Return a budget-window report; raise ReportNotFoundError for any other."""
        record = self.reports.get(report_id)
        if (
            record is None
            or record.kind is not ReportKind.ECONOMY
            or record.budget_window_size is None
        ):
            msg = f"no budget-window report {report_id}"
            raise ReportNotFoundError(msg)
        return record
