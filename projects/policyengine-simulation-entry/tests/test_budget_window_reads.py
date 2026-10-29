"""Reading budget-window reports: each year's progress, and totals summed on read."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from budget_window_fakes import WindowPath, finish_year
from economy_fakes import NOW, US_VERSION
from policyengine_simulation_entry.budget_window_reads import (
    BudgetWindowReportReads,
    StoredWindowResultError,
)
from policyengine_simulation_entry.budget_window_schemas import (
    BudgetWindowReportSubmissionRequest,
)
from policyengine_simulation_entry.economy_report_types import ReportNotFoundError
from policyengine_simulation_entry.economy_schemas import ReportStatus
from policyengine_simulation_persistence import (
    BudgetWindowStatus,
    NewBudgetWindowRun,
    RunError,
    RunErrorCode,
    RunStatus,
    RunTrigger,
    YearBudget,
)
from pydantic import ValidationError


def _reads(path: WindowPath) -> BudgetWindowReportReads:
    return BudgetWindowReportReads(
        simulations=path.simulations,
        reports=path.reports,
        windows=path.windows,
        catalog=path.catalog,
    )


def _budget(year: int, budgetary_impact: float, **changes: float) -> YearBudget:
    values = {
        "year": year,
        "budgetary_impact": budgetary_impact,
        "tax_revenue_impact": 2.0,
        "state_tax_revenue_impact": 0.5,
        "benefit_spending_impact": 1.0,
    }
    values.update(changes)
    return YearBudget(**values)  # type: ignore[arg-type]


def _finish_window(path: WindowPath, run_id, budgets: dict[int, float]) -> None:
    """Succeed every year with its budgetary impact, and the run itself."""
    while True:
        years = path.year_runs(run_id)
        unfinished = [
            report_run
            for report_run in years.values()
            if report_run.comparison_status is not RunStatus.SUCCEEDED
        ]
        if not unfinished:
            break
        for report_run in unfinished:
            finish_year(path.runs, report_run)
            path.windows.budgets[report_run.id] = _budget(
                report_run.year, budgets[report_run.year]
            )
            asyncio.run(
                path.budget_windows.start_years_after_call(
                    simulation_run_ids=None, report_run_id=report_run.id
                )
            )


def test_a_new_window_reads_its_started_and_queued_years() -> None:
    path = WindowPath(max_concurrent_years=2)
    report, run = path.submit_window(window_size=3)

    read = _reads(path).report_by_id(report.id)

    assert read.start_year == 2026
    assert read.window_size == 3
    # The first two years' calls have started; the third waits its turn.
    assert read.status is ReportStatus.RUNNING
    assert read.result is None
    assert read.run is not None
    assert read.run.id == run.id
    assert read.run.catalog_version_id == US_VERSION.id
    assert read.run.policyengine_version == "6.7.5"
    assert read.run.max_concurrent_years == 2
    assert read.run.progress == 0
    started = path.year_runs(run.id)
    assert [
        (year.year, year.status, year.stage, year.report_run_id)
        for year in read.run.years
    ] == [
        (2026, ReportStatus.RUNNING, "simulating", started[2026].id),
        (2027, ReportStatus.RUNNING, "simulating", started[2027].id),
        (2028, ReportStatus.PENDING, "queued", None),
    ]


def test_a_window_whose_years_have_not_started_calling_is_pending() -> None:
    path = WindowPath(max_concurrent_years=2)
    setup = path.prepare(window_size=2)
    # The run a submission creates, before it starts any year.
    run = path.windows.create_run(
        NewBudgetWindowRun(
            report_id=setup.report.id,
            tax_benefit_model_version_id=US_VERSION.id,
            trigger=RunTrigger.INITIAL,
            max_concurrent_years=2,
        ),
        first_check_at=NOW,
    )

    read = _reads(path).report_by_id(setup.report.id)

    assert read.status is ReportStatus.PENDING
    assert read.run is not None
    assert read.run.id == run.id
    assert {(year.status, year.stage) for year in read.run.years} == {
        (ReportStatus.PENDING, "queued")
    }


def test_a_window_whose_comparison_runs_reads_its_stage() -> None:
    path = WindowPath(max_concurrent_years=2)
    report, run = path.submit_window(window_size=3)
    first_year = path.year_runs(run.id)[2026]
    for run_id in (
        first_year.baseline_simulation_run_id,
        first_year.reform_simulation_run_id,
    ):
        path.runs.succeed(run_id)

    read = _reads(path).report_by_id(report.id)

    assert read.status is ReportStatus.RUNNING
    assert read.run is not None
    assert (read.run.years[0].status, read.run.years[0].stage) == (
        ReportStatus.RUNNING,
        "outputs",
    )


def test_a_finished_window_sums_its_years_exactly_when_read() -> None:
    path = WindowPath(max_concurrent_years=2)
    report, run = path.submit_window(window_size=3)
    _finish_window(path, run.id, {2026: 0.1, 2027: 0.2, 2028: 0.3})

    read = _reads(path).report_by_id(report.id)

    assert read.status is ReportStatus.SUCCEEDED
    assert read.run is not None
    assert read.run.progress == 100
    assert read.run.completed_at == NOW
    result = read.result
    assert result is not None
    assert (result.startYear, result.endYear, result.windowSize) == ("2026", "2028", 3)
    assert [impact.year for impact in result.annualImpacts] == ["2026", "2027", "2028"]
    assert [impact.budgetaryImpact for impact in result.annualImpacts] == [
        0.1,
        0.2,
        0.3,
    ]
    # Binary floats would make 0.6000000000000001.
    assert result.totals.budgetaryImpact == 0.6
    assert result.totals.year == "Total"
    first = result.annualImpacts[0]
    assert first.taxRevenueImpact == 2.0
    assert first.stateTaxRevenueImpact == 0.5
    assert first.federalTaxRevenueImpact == 1.5
    assert first.benefitSpendingImpact == 1.0
    assert result.totals.federalTaxRevenueImpact == 4.5


def test_a_failed_year_fails_the_window_before_the_run_is_finished() -> None:
    path = WindowPath(max_concurrent_years=2)
    report, run = path.submit_window(window_size=3)
    reform_run_id = path.year_runs(run.id)[2027].reform_simulation_run_id
    assert reform_run_id is not None
    path.runs.simulation_runs[reform_run_id] = replace(
        path.runs.simulation_runs[reform_run_id],
        status=RunStatus.FAILED,
        error_code="worker_timeout",
        error_message="The Modal function timed out.",
        completed_at=NOW,
    )

    read = _reads(path).report_by_id(report.id)

    assert read.status is ReportStatus.FAILED
    assert read.run is not None
    assert read.run.error is not None
    assert read.run.error.code == "worker_timeout"
    assert read.run.error.message == (
        "The 2027 calculation failed: The Modal function timed out."
    )
    assert read.run.years[1].status is ReportStatus.FAILED
    assert read.result is None


def test_a_failed_run_reads_its_own_error_and_its_years_never_started() -> None:
    path = WindowPath(max_concurrent_years=1)
    report, run = path.submit_window(window_size=3)
    path.windows.finish_run(
        run.id,
        BudgetWindowStatus.FAILED,
        RunError(RunErrorCode.INVALID_INPUT, "The 2027 calculation cannot start."),
    )

    read = _reads(path).report_by_id(report.id)

    assert read.status is ReportStatus.FAILED
    assert read.run is not None
    assert read.run.error is not None
    assert (read.run.error.code, read.run.error.message) == (
        "invalid_input",
        "The 2027 calculation cannot start.",
    )
    assert [year.status for year in read.run.years] == [
        ReportStatus.RUNNING,
        ReportStatus.NOT_RUN,
        ReportStatus.NOT_RUN,
    ]


def test_a_succeeded_window_missing_a_years_budget_is_a_stored_result_error() -> None:
    path = WindowPath(max_concurrent_years=2)
    report, run = path.submit_window(window_size=2)
    _finish_window(path, run.id, {2026: 1.0, 2027: 2.0})
    del path.windows.budgets[path.year_runs(run.id)[2027].id]

    with pytest.raises(StoredWindowResultError) as error:
        _reads(path).report_by_id(report.id)

    assert error.value.budget_window_run_id == run.id


def test_a_reports_runs_are_listed_newest_first() -> None:
    path = WindowPath()
    report, first = path.submit_window(window_size=2)
    rerun = asyncio.run(
        path.budget_windows.run_report(
            report.id,
            policyengine_version="6.7.5",
            trigger=RunTrigger.MANUAL,
            force=True,
        )
    )

    listed = _reads(path).report_runs(report.id)

    assert [run.id for run in listed.runs] == [rerun.id, first.id]
    assert listed.runs[0].force is True
    assert listed.runs[0].trigger == "manual"


def test_a_report_without_a_run_has_not_run() -> None:
    path = WindowPath()
    setup = path.prepare(window_size=2)

    read = _reads(path).report_by_id(setup.report.id)

    assert read.status is ReportStatus.NOT_RUN
    assert read.run is None
    assert read.result is None


def test_only_budget_window_reports_are_read() -> None:
    path = WindowPath()
    single_year, _ = asyncio.run(path.economy.submit(path.submission()))
    reads = _reads(path)

    for report_id in (single_year.id, uuid4()):
        with pytest.raises(ReportNotFoundError, match="budget-window report"):
            reads.report_by_id(report_id)
        with pytest.raises(ReportNotFoundError, match="budget-window report"):
            reads.report_runs(report_id)


def test_a_window_must_end_in_2099_or_earlier() -> None:
    values = {
        "country": "us",
        "policyengine_version": "6.7.5",
        "region_code": "us",
        "baseline": {},
        "reform": {},
    }

    ends_in_2099 = BudgetWindowReportSubmissionRequest.model_validate(
        {**values, "start_year": 2090, "window_size": 10}
    )
    assert ends_in_2099.window_size == 10
    with pytest.raises(ValidationError, match="2099 or earlier"):
        BudgetWindowReportSubmissionRequest.model_validate(
            {**values, "start_year": 2091, "window_size": 10}
        )
    for window_size in (0, 76):
        with pytest.raises(ValidationError, match="window_size"):
            BudgetWindowReportSubmissionRequest.model_validate(
                {**values, "start_year": 2026, "window_size": window_size}
            )
