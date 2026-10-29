"""An economy report's runs for any year of its window, and budget-window reports.

A budget-window report compares its simulations in each year of its window,
one report run per year; the single-year path neither runs nor reads it.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from budget_window_fakes import LABOR_SUPPLY_POLICY, EconomyPath
from economy_fakes import US_VERSION
from policyengine_simulation_entry.economy_report_types import (
    EconomyRequestError,
    ReportNotFoundError,
    SimulationChoice,
)
from policyengine_simulation_entry.economy_routes import EconomyReportReads
from policyengine_simulation_persistence import RunTrigger


def _run_year(path: EconomyPath, setup, year: int, **changes: object):
    values: dict[str, object] = {
        "year": year,
        "trigger": RunTrigger.INITIAL,
        "force": False,
        "budget_window_run_id": uuid4(),
    }
    values.update(changes)
    return asyncio.run(path.economy.run_year(setup, **values))  # type: ignore[arg-type]


def test_a_budget_window_report_records_its_window_without_running() -> None:
    path = EconomyPath()

    setup = path.prepare(window_size=3)

    assert setup.report.year == 2026
    assert setup.report.budget_window_size == 3
    assert setup.version == US_VERSION
    assert [simulation.policy_id for simulation in setup.simulations] == [
        None,
        setup.simulations[1].policy_id,
    ]
    assert path.runs.report_runs_by_id == {}
    assert path.dispatcher.requests == []


def test_a_budget_window_report_is_refused_before_anything_is_written() -> None:
    path = EconomyPath()

    with pytest.raises(EconomyRequestError, match="region"):
        path.prepare(region_code="state/zz")

    assert path.simulations.rows == {}
    assert path.reports.rows == {}


def test_a_year_runs_both_simulations_in_that_year() -> None:
    path = EconomyPath()
    setup = path.prepare()
    window_run_id = uuid4()

    report_run = _run_year(path, setup, 2027, budget_window_run_id=window_run_id)

    assert report_run.year == 2027
    assert report_run.report_id == setup.report.id
    assert report_run.budget_window_run_id == window_run_id
    assert report_run.tax_benefit_model_version_id == US_VERSION.id
    calls = path.simulation_calls()
    assert {call.year for call in calls} == {2027}
    assert {call.simulation_run_id for call in calls} == {
        report_run.baseline_simulation_run_id,
        report_run.reform_simulation_run_id,
    }


def test_each_year_has_its_own_runs_and_a_year_reuses_its_runs() -> None:
    path = EconomyPath()
    setup = path.prepare()

    first_year = _run_year(path, setup, 2026)
    second_year = _run_year(path, setup, 2027)
    again = _run_year(path, setup, 2027, trigger=RunTrigger.MANUAL)
    forced = _run_year(path, setup, 2027, trigger=RunTrigger.MANUAL, force=True)

    assert second_year.baseline_simulation_run_id != (
        first_year.baseline_simulation_run_id
    )
    assert again.baseline_simulation_run_id == second_year.baseline_simulation_run_id
    assert forced.baseline_simulation_run_id != second_year.baseline_simulation_run_id
    assert path.runs.simulation_runs[forced.baseline_simulation_run_id].attempt == 2


def test_a_system_year_is_a_report_run_of_its_own() -> None:
    """A ``system`` year is not the report's newest run, as a first open is."""
    path = EconomyPath()
    setup = path.prepare()
    first_year = _run_year(path, setup, 2026, trigger=RunTrigger.SYSTEM)

    second_year = _run_year(path, setup, 2027, trigger=RunTrigger.SYSTEM)

    assert second_year.id != first_year.id
    assert second_year.year == 2027


def test_a_labour_supply_year_calculates_its_runs_with_their_columns() -> None:
    path = EconomyPath()
    setup = path.prepare(reform=SimulationChoice(policy_id=LABOR_SUPPLY_POLICY))

    _run_year(path, setup, 2026)

    assert {call.labor_supply_response for call in path.simulation_calls()} == {True}


def test_a_year_whose_runs_succeeded_starts_its_comparison_at_once() -> None:
    path = EconomyPath()
    setup = path.prepare()
    first = _run_year(path, setup, 2027)
    for run_id in (first.baseline_simulation_run_id, first.reform_simulation_run_id):
        path.runs.succeed(run_id)

    again = _run_year(path, setup, 2027, trigger=RunTrigger.MANUAL)

    assert [call.report_run_id for call in path.comparison_calls()] == [
        first.id,
        again.id,
    ]
    assert {call.year for call in path.comparison_calls()} == {2027}


def test_the_single_year_path_does_not_run_a_budget_window_report() -> None:
    path = EconomyPath()
    setup = path.prepare()

    with pytest.raises(ReportNotFoundError, match="economy report"):
        asyncio.run(
            path.economy.run_report(
                setup.report.id,
                policyengine_version="6.7.5",
                trigger=RunTrigger.MANUAL,
                force=False,
            )
        )
    assert path.dispatcher.requests == []


def test_the_single_year_reads_do_not_find_a_budget_window_report() -> None:
    path = EconomyPath()
    setup = path.prepare()
    reads = EconomyReportReads(
        simulations=path.simulations,
        reports=path.reports,
        runs=path.runs,
        catalog=path.catalog,
        results=None,  # type: ignore[arg-type]
    )

    with pytest.raises(ReportNotFoundError, match="economy report"):
        reads.report_by_id(setup.report.id)
    with pytest.raises(ReportNotFoundError, match="economy report"):
        reads.report_runs(setup.report.id)


def test_a_single_year_report_is_set_up_as_before() -> None:
    path = EconomyPath()

    setup = path.prepare(window_size=None)
    report, report_run = asyncio.run(path.economy.submit(path.submission()))

    assert setup.report.budget_window_size is None
    assert report.budget_window_size is None
    assert report_run.year == 2026
    assert report_run.budget_window_run_id is None


def test_a_setup_reads_the_reports_simulations_on_a_version() -> None:
    path = EconomyPath()
    prepared = path.prepare()

    setup = asyncio.run(path.economy.report_setup(prepared.report, US_VERSION))

    assert setup == prepared
