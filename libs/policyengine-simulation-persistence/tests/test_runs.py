"""simulation_runs and report_runs: executions of simulations and reports."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from policyengine_simulation_persistence import (
    SUCCEEDED_RUN_REUSE_WINDOW,
    NewReportRun,
    ReportKind,
    ReportRunRecord,
    RunStatus,
    RunTrigger,
    SimulationRunRecord,
    create_simulation_engine,
)
from public_api_support import delete_public_api_row, superuser_engine
from report_support import (
    economy_pair,
    economy_report,
    household_baseline,
    report_store,
)
from run_support import (
    economy_run,
    household_run,
    run_store,
    set_simulation_run_status,
)
from simulation_support import (
    economy_simulation,
    household_simulation,
    simulation_store,
)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

pytestmark = pytest.mark.postgres


def test_an_economy_run_records_the_region_and_dataset_it_resolved(
    migrated_database, public_api_rows
) -> None:
    _, reform = economy_pair(migrated_database, public_api_rows)
    store = run_store(migrated_database)

    started = store.start_simulation_run(economy_run(reform, public_api_rows))

    run = started.run
    assert started.created is True
    assert isinstance(run, SimulationRunRecord)
    assert run.simulation_id == reform.id
    assert run.simulation_kind == "economy"
    assert run.tax_benefit_model_version_id == public_api_rows.catalog_version_id
    assert run.year == 2026
    assert run.attempt == 1
    assert run.region_code == "state/ca"
    assert run.dataset_id == public_api_rows.dataset_id
    assert run.status is RunStatus.PENDING
    assert run.trigger is RunTrigger.INITIAL
    assert run.modal_call_id is None
    assert run.completed_at is None
    assert store.get_simulation_run(run.id) == run


def test_a_run_that_exists_is_reused(migrated_database, public_api_rows) -> None:
    baseline, _ = economy_pair(migrated_database, public_api_rows)
    store = run_store(migrated_database)
    first = store.start_simulation_run(economy_run(baseline, public_api_rows))

    again = store.start_simulation_run(
        economy_run(baseline, public_api_rows, trigger=RunTrigger.MANUAL)
    )

    assert again.created is False
    assert again.run == first.run


def test_a_forced_rerun_adds_the_next_attempt(
    migrated_database, public_api_rows
) -> None:
    baseline, _ = economy_pair(migrated_database, public_api_rows)
    store = run_store(migrated_database)
    first = store.start_simulation_run(economy_run(baseline, public_api_rows))

    forced = store.start_simulation_run(
        economy_run(baseline, public_api_rows, trigger=RunTrigger.MANUAL), force=True
    )

    assert forced.created is True
    assert forced.run.attempt == first.run.attempt + 1
    assert forced.run.trigger is RunTrigger.MANUAL
    latest = store.latest_simulation_run(
        baseline.id, public_api_rows.catalog_version_id, 2026
    )
    assert latest == forced.run


LOCK_WAIT_TIMEOUT_SECONDS = 10


def _wait_until_a_statement_waits_for_a_lock(database) -> None:
    engine = superuser_engine(database.superuser_url)
    deadline = time.monotonic() + LOCK_WAIT_TIMEOUT_SECONDS
    try:
        with engine.connect() as connection:
            while time.monotonic() < deadline:
                waiting = connection.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock'"
                    )
                ).scalar_one()
                connection.rollback()
                if waiting:
                    return
                time.sleep(0.05)
    finally:
        engine.dispose()
    msg = "no statement waited for a lock"
    raise AssertionError(msg)


def test_a_forced_start_that_loses_the_insert_adds_the_attempt_after(
    migrated_database, public_api_rows
) -> None:
    """A forced caller always starts a run of its own.

    Another caller adds the attempt the forced caller read as next, between
    that read and the forced caller's insert. A forced start (a labour supply
    report's) must not receive the other caller's run, which may have been
    started without what the forced caller needs.
    """
    baseline, _ = economy_pair(migrated_database, public_api_rows)
    store = run_store(migrated_database)
    first = store.start_simulation_run(economy_run(baseline, public_api_rows))
    other_caller_engine = superuser_engine(migrated_database.superuser_url)
    try:
        with other_caller_engine.connect() as other_caller:
            other_transaction = other_caller.begin()
            other_caller.execute(
                text(
                    "INSERT INTO simulation.simulation_runs (id, simulation_id, "
                    "simulation_kind, tax_benefit_model_version_id, year, attempt, "
                    "region_code, dataset_id, trigger, status, created_at, "
                    "updated_at) SELECT gen_random_uuid(), simulation_id, "
                    "simulation_kind, tax_benefit_model_version_id, year, "
                    "attempt + 1, region_code, dataset_id, 'initial', 'pending', "
                    "now(), now() FROM simulation.simulation_runs WHERE id = :run_id"
                ),
                {"run_id": first.run.id},
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                forced_start = pool.submit(
                    store.start_simulation_run,
                    economy_run(baseline, public_api_rows, trigger=RunTrigger.MANUAL),
                    force=True,
                )
                _wait_until_a_statement_waits_for_a_lock(migrated_database)
                other_transaction.commit()
                forced = forced_start.result(timeout=LOCK_WAIT_TIMEOUT_SECONDS)
    finally:
        other_caller_engine.dispose()

    assert forced.created is True
    assert forced.run.attempt == first.run.attempt + 2
    assert forced.run.trigger is RunTrigger.MANUAL


def test_a_failed_run_is_not_reused(migrated_database, public_api_rows) -> None:
    baseline, _ = economy_pair(migrated_database, public_api_rows)
    store = run_store(migrated_database)
    first = store.start_simulation_run(economy_run(baseline, public_api_rows))
    set_simulation_run_status(migrated_database, first.run.id, "failed")

    again = store.start_simulation_run(economy_run(baseline, public_api_rows))

    assert again.created is True
    assert again.run.attempt == 2


@pytest.mark.parametrize(
    ("completed_days_ago", "reused"),
    [(27, True), (29, False)],
    ids=["within-28-days", "older-than-28-days"],
)
def test_a_succeeded_run_is_reused_only_within_28_days(
    migrated_database, public_api_rows, completed_days_ago, reused
) -> None:
    baseline, _ = economy_pair(migrated_database, public_api_rows)
    store = run_store(migrated_database)
    first = store.start_simulation_run(economy_run(baseline, public_api_rows))
    set_simulation_run_status(
        migrated_database,
        first.run.id,
        "succeeded",
        completed_days_ago=completed_days_ago,
    )

    again = store.start_simulation_run(economy_run(baseline, public_api_rows))

    assert again.created is not reused
    assert again.run.attempt == (1 if reused else 2)
    assert timedelta(days=28) == SUCCEEDED_RUN_REUSE_WINDOW


def test_each_catalog_version_and_year_has_its_own_runs(
    migrated_database, public_api_rows
) -> None:
    baseline, _ = economy_pair(migrated_database, public_api_rows)
    store = run_store(migrated_database)
    first = store.start_simulation_run(economy_run(baseline, public_api_rows))

    newer = store.start_simulation_run(
        economy_run(
            baseline,
            public_api_rows,
            tax_benefit_model_version_id=public_api_rows.newer_catalog_version_id,
            dataset_id=public_api_rows.newer_dataset_id,
        )
    )
    next_year = store.start_simulation_run(
        economy_run(baseline, public_api_rows, year=2027)
    )

    assert len({first.run.id, newer.run.id, next_year.run.id}) == 3
    assert newer.created is True
    assert next_year.created is True
    assert newer.run.attempt == next_year.run.attempt == 1


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"dataset_attribute": "newer_dataset_id"}, "fk_simulation_runs_dataset"),
        ({"region_code": None}, "population_matches_kind"),
        ({"dataset_attribute": None}, "population_matches_kind"),
    ],
    ids=[
        "dataset-of-another-version",
        "economy-without-region",
        "economy-without-dataset",
    ],
)
def test_an_economy_run_uses_only_its_own_versions_dataset(
    migrated_database, public_api_rows, overrides, constraint
) -> None:
    baseline, _ = economy_pair(migrated_database, public_api_rows)
    values = dict(overrides)
    if "dataset_attribute" in values:
        attribute = values.pop("dataset_attribute")
        values["dataset_id"] = (
            None if attribute is None else getattr(public_api_rows, attribute)
        )

    with pytest.raises(IntegrityError, match=constraint):
        run_store(migrated_database).start_simulation_run(
            economy_run(baseline, public_api_rows, **values)
        )


def test_an_economy_run_needs_its_region_in_its_catalog_version(
    migrated_database, public_api_rows
) -> None:
    new_york = simulation_store(migrated_database).create(
        economy_simulation(public_api_rows, region_code="state/ny")
    )

    with pytest.raises(IntegrityError, match="fk_simulation_runs_region"):
        run_store(migrated_database).start_simulation_run(
            economy_run(new_york, public_api_rows, region_code="state/ny")
        )


def test_an_economy_run_uses_its_simulations_region(
    migrated_database, public_api_rows
) -> None:
    baseline, _ = economy_pair(migrated_database, public_api_rows)

    with pytest.raises(ValueError, match="region_code"):
        run_store(migrated_database).start_simulation_run(
            economy_run(baseline, public_api_rows, region_code="state/ny")
        )


def test_a_household_run_has_no_region_or_dataset(
    migrated_database, public_api_rows
) -> None:
    simulation = household_baseline(migrated_database, public_api_rows)
    store = run_store(migrated_database)

    started = store.start_simulation_run(household_run(simulation, public_api_rows))
    assert started.run.simulation_kind == "household"
    assert started.run.region_code is None

    with pytest.raises(IntegrityError, match="population_matches_kind"):
        store.start_simulation_run(
            household_run(
                simulation,
                public_api_rows,
                year=2027,
                region_code="state/ca",
                dataset_id=public_api_rows.dataset_id,
            )
        )


def test_a_run_of_an_unknown_simulation_is_refused(
    migrated_database, public_api_rows
) -> None:
    baseline, _ = economy_pair(migrated_database, public_api_rows)

    with pytest.raises(LookupError, match="simulation"):
        run_store(migrated_database).start_simulation_run(
            economy_run(baseline, public_api_rows, simulation_id=uuid4())
        )


def test_the_public_api_cannot_delete_a_dataset_a_run_used(
    migrated_database, public_api_rows
) -> None:
    baseline, _ = economy_pair(migrated_database, public_api_rows)
    run_store(migrated_database).start_simulation_run(
        economy_run(baseline, public_api_rows)
    )

    with pytest.raises(IntegrityError, match="violates foreign key constraint"):
        delete_public_api_row(
            migrated_database.superuser_url, "datasets", public_api_rows.dataset_id
        )


def _economy_report_with_runs(database, rows):
    baseline, reform = economy_pair(database, rows)
    report = report_store(database).create(economy_report(baseline, reform))
    store = run_store(database)
    baseline_run = store.start_simulation_run(economy_run(baseline, rows)).run
    reform_run = store.start_simulation_run(economy_run(reform, rows)).run
    return report, baseline_run, reform_run


def test_an_economy_report_run_joins_two_runs_and_waits_for_its_comparison(
    migrated_database, public_api_rows
) -> None:
    report, baseline_run, reform_run = _economy_report_with_runs(
        migrated_database, public_api_rows
    )
    store = run_store(migrated_database)

    report_run = store.create_report_run(
        NewReportRun(
            report_id=report.id,
            baseline_simulation_run_id=baseline_run.id,
            reform_simulation_run_id=reform_run.id,
            trigger=RunTrigger.INITIAL,
        )
    )

    assert isinstance(report_run, ReportRunRecord)
    assert report_run.report_kind == "economy"
    assert report_run.tax_benefit_model_version_id == (
        public_api_rows.catalog_version_id
    )
    assert report_run.year == 2026
    assert report_run.comparison_status is RunStatus.PENDING
    assert report_run.comparison_modal_call_id is None
    assert store.get_report_run(report_run.id) == report_run
    assert store.latest_report_run(report.id) == report_run


def test_both_runs_of_a_report_run_share_its_version_and_year(
    migrated_database, public_api_rows
) -> None:
    report, baseline_run, _ = _economy_report_with_runs(
        migrated_database, public_api_rows
    )
    store = run_store(migrated_database)
    reform = report_store(migrated_database).get(report.id)
    assert reform is not None
    later_reform_run = store.start_simulation_run(
        economy_run(
            simulation_store(migrated_database).get(reform.reform_simulation_id),
            public_api_rows,
            year=2027,
        )
    ).run

    with pytest.raises(IntegrityError, match="fk_report_runs_reform_simulation_run"):
        store.create_report_run(
            NewReportRun(
                report_id=report.id,
                baseline_simulation_run_id=baseline_run.id,
                reform_simulation_run_id=later_reform_run.id,
                trigger=RunTrigger.INITIAL,
            )
        )


def test_a_report_run_uses_only_runs_of_the_reports_simulations(
    migrated_database, public_api_rows
) -> None:
    report, baseline_run, reform_run = _economy_report_with_runs(
        migrated_database, public_api_rows
    )

    with pytest.raises(ValueError, match="report's simulations"):
        run_store(migrated_database).create_report_run(
            NewReportRun(
                report_id=report.id,
                baseline_simulation_run_id=reform_run.id,
                reform_simulation_run_id=baseline_run.id,
                trigger=RunTrigger.INITIAL,
            )
        )


def test_an_economy_report_run_needs_its_reform_run(
    migrated_database, public_api_rows
) -> None:
    report, baseline_run, _ = _economy_report_with_runs(
        migrated_database, public_api_rows
    )

    with pytest.raises(ValueError, match="report's simulations"):
        run_store(migrated_database).create_report_run(
            NewReportRun(
                report_id=report.id,
                baseline_simulation_run_id=baseline_run.id,
                trigger=RunTrigger.INITIAL,
            )
        )


def test_a_household_report_run_has_no_comparison_step(
    migrated_database, public_api_rows
) -> None:
    store = simulation_store(migrated_database)
    baseline = household_baseline(migrated_database, public_api_rows)
    reform = store.create(
        household_simulation(public_api_rows, policy_id=public_api_rows.us_policy_id)
    )
    report = report_store(migrated_database).create(
        economy_report(baseline, reform, kind=ReportKind.HOUSEHOLD)
    )
    runs = run_store(migrated_database)
    baseline_run = runs.start_simulation_run(
        household_run(baseline, public_api_rows)
    ).run
    reform_run = runs.start_simulation_run(household_run(reform, public_api_rows)).run

    report_run = runs.create_report_run(
        NewReportRun(
            report_id=report.id,
            baseline_simulation_run_id=baseline_run.id,
            reform_simulation_run_id=reform_run.id,
            trigger=RunTrigger.SYSTEM,
        )
    )

    assert report_run.report_kind == "household"
    assert report_run.comparison_status is None
    assert report_run.trigger is RunTrigger.SYSTEM


def test_a_single_simulation_household_report_run_has_no_reform_run(
    migrated_database, public_api_rows
) -> None:
    baseline = household_baseline(migrated_database, public_api_rows)
    report = report_store(migrated_database).create(
        economy_report(
            baseline, baseline, kind=ReportKind.HOUSEHOLD, reform_simulation_id=None
        )
    )
    runs = run_store(migrated_database)
    baseline_run = runs.start_simulation_run(
        household_run(baseline, public_api_rows)
    ).run

    report_run = runs.create_report_run(
        NewReportRun(
            report_id=report.id,
            baseline_simulation_run_id=baseline_run.id,
            trigger=RunTrigger.INITIAL,
        )
    )

    assert report_run.reform_simulation_run_id is None


def test_the_newest_report_run_is_the_reports_current_run(
    migrated_database, public_api_rows
) -> None:
    report, baseline_run, reform_run = _economy_report_with_runs(
        migrated_database, public_api_rows
    )
    store = run_store(migrated_database)
    new_report_run = NewReportRun(
        report_id=report.id,
        baseline_simulation_run_id=baseline_run.id,
        reform_simulation_run_id=reform_run.id,
        trigger=RunTrigger.INITIAL,
    )
    store.create_report_run(new_report_run)

    rerun = store.create_report_run(
        NewReportRun(
            report_id=report.id,
            baseline_simulation_run_id=baseline_run.id,
            reform_simulation_run_id=reform_run.id,
            trigger=RunTrigger.MANUAL,
        )
    )

    assert store.latest_report_run(report.id) == rerun
    assert store.latest_report_run(uuid4()) is None


def test_a_report_run_of_an_unknown_report_is_refused(
    migrated_database, public_api_rows
) -> None:
    _, baseline_run, reform_run = _economy_report_with_runs(
        migrated_database, public_api_rows
    )

    with pytest.raises(LookupError, match="report"):
        run_store(migrated_database).create_report_run(
            NewReportRun(
                report_id=uuid4(),
                baseline_simulation_run_id=baseline_run.id,
                reform_simulation_run_id=reform_run.id,
                trigger=RunTrigger.INITIAL,
            )
        )


@pytest.mark.parametrize("table_name", ["simulation_runs", "report_runs"])
def test_the_runtime_role_updates_runs_but_cannot_delete_them(
    migrated_database, public_api_rows, table_name
) -> None:
    report, baseline_run, reform_run = _economy_report_with_runs(
        migrated_database, public_api_rows
    )
    run_store(migrated_database).create_report_run(
        NewReportRun(
            report_id=report.id,
            baseline_simulation_run_id=baseline_run.id,
            reform_simulation_run_id=reform_run.id,
            trigger=RunTrigger.INITIAL,
        )
    )
    engine = create_simulation_engine(migrated_database.runtime_url)
    try:
        with engine.begin() as connection:
            updated = connection.execute(
                text(f"UPDATE simulation.{table_name} SET updated_at = now()")  # noqa: S608  # fixed table names
            )
        assert updated.rowcount >= 1
        with (
            pytest.raises(ProgrammingError, match="permission denied"),
            engine.begin() as connection,
        ):
            connection.execute(text(f"DELETE FROM simulation.{table_name}"))  # noqa: S608  # fixed table names
    finally:
        engine.dispose()
