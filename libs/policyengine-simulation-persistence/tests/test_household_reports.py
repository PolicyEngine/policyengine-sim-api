"""Household reports: two household simulations' runs, sharing one Modal call."""

from __future__ import annotations

from uuid import uuid4

import pytest
from policyengine_simulation_persistence import (
    HouseholdCalculationStore,
    NewReport,
    NewReportRun,
    ReportKind,
    RunStatus,
    RunTrigger,
    create_simulation_engine,
)
from report_support import economy_pair, economy_report
from run_support import household_run
from simulation_support import household_simulation

pytestmark = pytest.mark.postgres


def household_store(database) -> HouseholdCalculationStore:
    """Return a household calculation store that connects as the runtime role."""
    return HouseholdCalculationStore(create_simulation_engine(database.runtime_url))


def _report_with_run(store, rows, *, reform_policy_id=None, trigger=None):
    """Create a 2026 household report and one run of it; return both."""
    baseline = store.find_or_create_simulation(household_simulation(rows))
    reform = (
        None
        if reform_policy_id is None
        else store.find_or_create_simulation(
            household_simulation(rows, policy_id=reform_policy_id)
        )
    )
    report = store.create_report(
        NewReport(
            kind=ReportKind.HOUSEHOLD,
            country="us",
            baseline_simulation_id=baseline.id,
            reform_simulation_id=None if reform is None else reform.id,
            year=2026,
        )
    )
    baseline_run = store.start_run(household_run(baseline, rows)).run
    reform_run = (
        None if reform is None else store.start_run(household_run(reform, rows)).run
    )
    report_run = store.create_report_run(
        NewReportRun(
            report_id=report.id,
            baseline_simulation_run_id=baseline_run.id,
            reform_simulation_run_id=None if reform_run is None else reform_run.id,
            trigger=trigger or RunTrigger.INITIAL,
        )
    )
    return report, report_run


def test_a_household_report_reads_back_with_both_calculations(
    migrated_database, public_api_rows
) -> None:
    store = household_store(migrated_database)
    report, report_run = _report_with_run(
        store, public_api_rows, reform_policy_id=public_api_rows.us_policy_id
    )

    read = store.read_report(report.id)

    assert read.report.id == report.id
    assert read.report_run.id == report_run.id
    assert read.baseline.run.id == report_run.baseline_simulation_run_id
    assert read.baseline.simulation.policy_id is None
    assert read.reform.run.id == report_run.reform_simulation_run_id
    assert read.reform.simulation.policy_id == public_api_rows.us_policy_id
    assert read.baseline.run.status is RunStatus.PENDING
    assert read.baseline.output is None


def test_a_baseline_only_household_report_has_no_reform(
    migrated_database, public_api_rows
) -> None:
    store = household_store(migrated_database)
    report, _ = _report_with_run(store, public_api_rows)

    read = store.read_report(report.id)

    assert read.report.reform_simulation_id is None
    assert read.reform is None


def test_a_report_reads_its_newest_run_unless_another_is_named(
    migrated_database, public_api_rows
) -> None:
    store = household_store(migrated_database)
    report, first_run = _report_with_run(store, public_api_rows)
    second_run = store.create_report_run(
        NewReportRun(
            report_id=report.id,
            baseline_simulation_run_id=first_run.baseline_simulation_run_id,
            trigger=RunTrigger.MANUAL,
        )
    )

    assert store.read_report(report.id).report_run.id == second_run.id
    assert (
        store.read_report(report.id, report_run_id=first_run.id).report_run.id
        == first_run.id
    )


def test_a_report_without_runs_reads_back_without_calculations(
    migrated_database, public_api_rows
) -> None:
    store = household_store(migrated_database)
    baseline = store.find_or_create_simulation(household_simulation(public_api_rows))
    report = store.create_report(
        NewReport(
            kind=ReportKind.HOUSEHOLD,
            country="us",
            baseline_simulation_id=baseline.id,
            year=2026,
        )
    )

    read = store.read_report(report.id)

    assert read.report_run is None
    assert read.baseline is None
    assert read.reform is None
    assert store.get_report(report.id) == report
    assert store.get_simulation(baseline.id) == baseline


def test_only_household_reports_and_their_own_runs_are_read(
    migrated_database, public_api_rows
) -> None:
    store = household_store(migrated_database)
    report, _ = _report_with_run(store, public_api_rows)
    other_report, other_run = _report_with_run(
        store, public_api_rows, reform_policy_id=public_api_rows.us_policy_id
    )
    baseline, reform = economy_pair(migrated_database, public_api_rows)
    economy = store.create_report(economy_report(baseline, reform))

    assert store.read_report(economy.id) is None
    assert store.get_report(economy.id) is None
    assert store.read_report(uuid4()) is None
    assert store.read_report(report.id, report_run_id=other_run.id) is None
    assert store.read_report(other_report.id, report_run_id=other_run.id) is not None
