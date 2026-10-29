"""Budget-window reports: one report run per year, at most a few years at once."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from budget_window_fakes import (
    LABOR_SUPPLY_POLICY,
    WindowPath,
    finish_year,
)
from economy_fakes import NOW, US_VERSION
from policyengine_simulation_contract.economy_runs import (
    BUDGET_WINDOW_MAX_CONCURRENT_YEARS,
)
from policyengine_simulation_entry.budget_window_reports import (
    BUDGET_WINDOW_CHECK_INTERVAL,
    DEFAULT_MAX_CONCURRENT_YEARS,
    SCHEDULING_LEASE,
)
from policyengine_simulation_entry.economy_report_types import (
    EconomyRequestError,
    ReportNotFoundError,
    SimulationChoice,
)
from policyengine_simulation_persistence import (
    BudgetWindowStatus,
    NewBudgetWindowRun,
    RunStatus,
    RunTrigger,
)


def _rerun(path: WindowPath, report_id, **changes: object):
    values: dict[str, object] = {
        "policyengine_version": "6.7.5",
        "trigger": RunTrigger.MANUAL,
        "force": False,
    }
    values.update(changes)
    return asyncio.run(path.budget_windows.run_report(report_id, **values))  # type: ignore[arg-type]


def _start_years_after(path: WindowPath, **reported: object):
    values: dict[str, object] = {"simulation_run_ids": None, "report_run_id": None}
    values.update(reported)
    return asyncio.run(path.budget_windows.start_years_after_call(**values))  # type: ignore[arg-type]


def test_a_submission_starts_its_first_years_up_to_the_cap() -> None:
    path = WindowPath(max_concurrent_years=2)

    report, run = path.submit_window(window_size=4)

    assert report.year == 2026
    assert report.budget_window_size == 4
    assert run.report_id == report.id
    assert run.trigger is RunTrigger.INITIAL
    assert run.tax_benefit_model_version_id == US_VERSION.id
    assert run.max_concurrent_years == 2
    assert run.status is BudgetWindowStatus.RUNNING
    assert sorted(path.year_runs(run.id)) == [2026, 2027]
    assert {call.year for call in path.simulation_calls()} == {2026, 2027}
    assert run.next_check_at == NOW + BUDGET_WINDOW_CHECK_INTERVAL
    assert run.scheduling_claimed_until is None


def test_a_years_recorded_comparison_starts_the_next_year() -> None:
    path = WindowPath(max_concurrent_years=2)
    _, run = path.submit_window(window_size=4)
    first_year = path.year_runs(run.id)[2026]
    finish_year(path.runs, first_year)

    started = _start_years_after(path, report_run_id=first_year.id)

    assert started == 1
    assert sorted(path.year_runs(run.id)) == [2026, 2027, 2028]
    assert {call.year for call in path.simulation_calls()} == {2026, 2027, 2028}


def test_a_year_whose_comparison_is_still_running_keeps_its_place() -> None:
    path = WindowPath(max_concurrent_years=2)
    _, run = path.submit_window(window_size=4)
    first_year = path.year_runs(run.id)[2026]
    for run_id in (
        first_year.baseline_simulation_run_id,
        first_year.reform_simulation_run_id,
    ):
        path.runs.succeed(run_id)

    started = _start_years_after(
        path,
        simulation_run_ids=[
            first_year.baseline_simulation_run_id,
            first_year.reform_simulation_run_id,
        ],
    )

    assert started == 0
    assert sorted(path.year_runs(run.id)) == [2026, 2027]


def test_every_year_succeeding_finishes_the_run() -> None:
    path = WindowPath(max_concurrent_years=2)
    _, run = path.submit_window(window_size=2)
    for year_run in path.year_runs(run.id).values():
        finish_year(path.runs, year_run)

    _start_years_after(path, report_run_id=path.year_runs(run.id)[2027].id)

    finished = path.windows.get_run(run.id)
    assert finished is not None
    assert finished.status is BudgetWindowStatus.SUCCEEDED
    assert finished.completed_at == NOW


def test_a_failed_year_fails_the_run_and_starts_no_more_years() -> None:
    path = WindowPath(max_concurrent_years=2)
    _, run = path.submit_window(window_size=4)
    failed_year = path.year_runs(run.id)[2027]
    reform_run_id = failed_year.reform_simulation_run_id
    assert reform_run_id is not None
    path.runs.simulation_runs[reform_run_id] = replace(
        path.runs.simulation_runs[reform_run_id],
        status=RunStatus.FAILED,
        error_code="worker_error",
        error_message="The Modal function raised RuntimeError.",
        completed_at=NOW,
    )
    finish_year(path.runs, path.year_runs(run.id)[2026])

    _start_years_after(path, simulation_run_ids=[reform_run_id])

    failed = path.windows.get_run(run.id)
    assert failed is not None
    assert failed.status is BudgetWindowStatus.FAILED
    assert failed.error_code == "worker_error"
    assert failed.error_message == (
        "The 2027 calculation failed: The Modal function raised RuntimeError."
    )
    assert sorted(path.year_runs(run.id)) == [2026, 2027]


def test_a_year_that_cannot_start_fails_the_run() -> None:
    path = WindowPath(max_concurrent_years=1)
    _, run = path.submit_window(window_size=3)
    finish_year(path.runs, path.year_runs(run.id)[2026])
    del path.catalog.populations["state/ca"]

    _start_years_after(path, report_run_id=path.year_runs(run.id)[2026].id)

    failed = path.windows.get_run(run.id)
    assert failed is not None
    assert failed.status is BudgetWindowStatus.FAILED
    assert failed.error_code == "invalid_input"
    assert failed.error_message is not None
    assert failed.error_message.startswith("The 2027 calculation cannot start:")
    assert sorted(path.year_runs(run.id)) == [2026]


def test_a_run_another_caller_is_scheduling_is_left_to_it() -> None:
    path = WindowPath(max_concurrent_years=2)
    _, run = path.submit_window(window_size=4)
    finish_year(path.runs, path.year_runs(run.id)[2026])
    path.windows.claims_refused = True

    started = _start_years_after(path, report_run_id=path.year_runs(run.id)[2026].id)

    assert started == 0
    assert sorted(path.year_runs(run.id)) == [2026, 2027]


def test_a_claim_is_released_with_the_next_check_even_when_starting_fails() -> None:
    path = WindowPath(max_concurrent_years=2)
    _, run = path.submit_window(window_size=4)
    finish_year(path.runs, path.year_runs(run.id)[2026])

    def refuse_to_start(*_args, **_kwargs):
        msg = "the database dropped the connection"
        raise ConnectionError(msg)

    path.runs.start_simulation_run = refuse_to_start  # type: ignore[method-assign]

    with pytest.raises(ConnectionError):
        _start_years_after(path, report_run_id=path.year_runs(run.id)[2026].id)

    released = path.windows.get_run(run.id)
    assert released is not None
    assert released.status is BudgetWindowStatus.RUNNING
    assert released.scheduling_claimed_until is None
    assert released.next_check_at == NOW + BUDGET_WINDOW_CHECK_INTERVAL


def test_a_claim_lasts_its_lease() -> None:
    assert timedelta(0) < SCHEDULING_LEASE <= BUDGET_WINDOW_CHECK_INTERVAL


def test_the_cap_is_the_one_the_modal_functions_have_room_for() -> None:
    assert DEFAULT_MAX_CONCURRENT_YEARS == BUDGET_WINDOW_MAX_CONCURRENT_YEARS == 5


def test_a_rerun_reuses_each_years_runs_unless_forced() -> None:
    path = WindowPath(max_concurrent_years=2)
    report, first = path.submit_window(window_size=2)
    calls_before = len(path.simulation_calls())

    rerun = _rerun(path, report.id)
    forced = _rerun(path, report.id, force=True)

    assert rerun.id != first.id
    assert rerun.trigger is RunTrigger.MANUAL
    assert forced.force is True
    first_years, rerun_years, forced_years = (
        path.year_runs(run.id) for run in (first, rerun, forced)
    )
    assert {
        year: run.baseline_simulation_run_id for year, run in rerun_years.items()
    } == {year: run.baseline_simulation_run_id for year, run in first_years.items()}
    assert all(
        forced_years[year].baseline_simulation_run_id
        != first_years[year].baseline_simulation_run_id
        for year in (2026, 2027)
    )
    # Only the forced run's years started calls: two years of two simulations.
    assert len(path.simulation_calls()) == calls_before + 4
    assert path.windows.latest_run(report.id) == path.windows.get_run(forced.id)


def test_a_system_run_of_a_report_that_has_a_run_returns_its_newest() -> None:
    path = WindowPath()
    report, first = path.submit_window(window_size=2)
    path.dispatcher.requests.clear()

    again = _rerun(path, report.id, trigger=RunTrigger.SYSTEM)

    assert again.id == first.id
    assert path.dispatcher.requests == []


def test_a_system_run_of_a_report_without_one_calculates_it() -> None:
    path = WindowPath()
    setup = path.prepare(window_size=2)

    run = _rerun(path, setup.report.id, trigger=RunTrigger.SYSTEM)

    assert run.trigger is RunTrigger.SYSTEM
    assert sorted(path.year_runs(run.id)) == [2026, 2027]
    assert {report_run.trigger for report_run in path.year_runs(run.id).values()} == {
        RunTrigger.SYSTEM
    }


def test_a_rerun_needs_a_budget_window_report_and_a_rerun_trigger() -> None:
    path = WindowPath()
    report, _ = asyncio.run(path.economy.submit(path.submission()))
    window_report, _ = path.submit_window(window_size=2)

    with pytest.raises(ReportNotFoundError, match="budget-window report"):
        _rerun(path, report.id)
    with pytest.raises(ReportNotFoundError, match="budget-window report"):
        _rerun(path, uuid4())
    with pytest.raises(EconomyRequestError, match="manual or system"):
        _rerun(path, window_report.id, trigger=RunTrigger.INITIAL)
    with pytest.raises(EconomyRequestError, match="catalog"):
        _rerun(path, window_report.id, policyengine_version="9.9.9")


def test_a_submission_that_cannot_run_is_refused_before_anything_starts() -> None:
    path = WindowPath()

    with pytest.raises(EconomyRequestError, match="region"):
        path.submit_window(region_code="state/zz")

    assert path.reports.rows == {}
    assert path.windows.rows == {}
    assert path.dispatcher.requests == []


def test_a_labour_supply_window_calculates_every_year_with_its_columns() -> None:
    path = WindowPath(max_concurrent_years=3)

    path.submit_window(
        window_size=3, reform=SimulationChoice(policy_id=LABOR_SUPPLY_POLICY)
    )

    calls = path.simulation_calls()
    assert {call.year for call in calls} == {2026, 2027, 2028}
    assert {call.labor_supply_response for call in calls} == {True}


def test_the_safety_check_starts_the_years_of_runs_left_waiting() -> None:
    path = WindowPath(max_concurrent_years=2)
    setup = path.prepare(window_size=3)
    stranded = path.windows.create_run(
        NewBudgetWindowRun(
            report_id=setup.report.id,
            tax_benefit_model_version_id=US_VERSION.id,
            trigger=RunTrigger.INITIAL,
            max_concurrent_years=2,
        ),
        first_check_at=NOW + BUDGET_WINDOW_CHECK_INTERVAL,
    )

    early = asyncio.run(path.budget_windows.resume_waiting_work(now=NOW))
    started = asyncio.run(
        path.budget_windows.resume_waiting_work(now=NOW + BUDGET_WINDOW_CHECK_INTERVAL)
    )

    assert early == 0
    assert started == 2
    assert sorted(path.year_runs(stranded.id)) == [2026, 2027]


def test_one_run_that_cannot_resume_does_not_stop_the_others() -> None:
    path = WindowPath(max_concurrent_years=1)
    broken_setup = path.prepare(window_size=2)
    working_setup = path.prepare(window_size=2, region_code="us")
    new_runs = [
        path.windows.create_run(
            NewBudgetWindowRun(
                report_id=setup.report.id,
                tax_benefit_model_version_id=US_VERSION.id,
                trigger=RunTrigger.INITIAL,
                max_concurrent_years=1,
            ),
            first_check_at=NOW,
        )
        for setup in (broken_setup, working_setup)
    ]
    del path.reports.rows[broken_setup.report.id]

    started = asyncio.run(path.budget_windows.resume_waiting_work(now=NOW))

    assert started == 1
    assert path.year_runs(new_runs[0].id) == {}
    assert sorted(path.year_runs(new_runs[1].id)) == [2026]
