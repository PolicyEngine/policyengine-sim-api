"""budget_window_runs: budget-window calculations, one report run per year."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from budget_window_support import (
    WINDOW_SIZE,
    WINDOW_START,
    budget_window_report,
    budget_window_store,
    new_budget_window_run,
    write_year_budget,
    year_report_run,
)
from migration_support import alembic_config, migration_cycle_failures
from policyengine_simulation_persistence import (
    ONE_REPORT_RUN_PER_YEAR,
    BudgetWindowStatus,
    NewBudgetWindowRun,
    NewReport,
    ReportKind,
    RunError,
    RunErrorCode,
    RunStatus,
    RunTrigger,
    YearBudget,
    create_simulation_engine,
)
from public_api_support import superuser_engine
from report_support import economy_pair, economy_report, report_store
from run_support import set_simulation_run_status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from transition_support import ManualClock

pytestmark = pytest.mark.postgres

FIRST_CHECK = timedelta(minutes=5)


def test_the_budget_window_revision_matches_its_tables_and_downgrades(
    disposable_database,
) -> None:
    config = alembic_config()

    assert migration_cycle_failures(config, check=True) == []
    command.downgrade(config, "0004")
    command.upgrade(config, "head")
    command.check(config)


def test_a_budget_window_report_records_its_window_size(
    migrated_database, public_api_rows
) -> None:
    report, _ = budget_window_report(migrated_database, public_api_rows)

    assert report.year == WINDOW_START
    assert report.budget_window_size == WINDOW_SIZE
    assert report_store(migrated_database).get(report.id) == report


def test_a_single_year_report_has_no_window(migrated_database, public_api_rows) -> None:
    baseline, reform = economy_pair(migrated_database, public_api_rows)

    report = report_store(migrated_database).create(economy_report(baseline, reform))

    assert report.budget_window_size is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"budget_window_size": 0}, "between 1 and 75"),
        ({"budget_window_size": 76}, "between 1 and 75"),
        ({"budget_window_size": 3, "year": 2199}, "2200"),
        ({"budget_window_size": 3, "kind": ReportKind.HOUSEHOLD}, "economy"),
    ],
)
def test_a_window_size_is_checked_before_the_database(overrides, message) -> None:
    values: dict[str, object] = {
        "kind": ReportKind.ECONOMY,
        "country": "us",
        "baseline_simulation_id": uuid4(),
        "reform_simulation_id": uuid4(),
        "year": 2026,
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        NewReport(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("window_size", [0, 76])
def test_the_database_checks_the_window_size(
    migrated_database, public_api_rows, window_size
) -> None:
    report, _ = budget_window_report(migrated_database, public_api_rows)
    engine = superuser_engine(migrated_database.superuser_url)
    try:
        with (
            pytest.raises(IntegrityError, match="ck_reports_budget_window_size"),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    "INSERT INTO simulation.reports (id, kind, country, "
                    "baseline_simulation_id, reform_simulation_id, year, "
                    "budget_window_size) VALUES (:id, 'economy', 'us', "
                    ":baseline, :reform, 2026, :size)"
                ),
                {
                    "id": uuid4(),
                    "baseline": report.baseline_simulation_id,
                    "reform": report.reform_simulation_id,
                    "size": window_size,
                },
            )
    finally:
        engine.dispose()


def test_a_budget_window_run_starts_running_and_due_a_check(
    migrated_database, public_api_rows
) -> None:
    report, _ = budget_window_report(migrated_database, public_api_rows)
    clock = ManualClock()
    store = budget_window_store(migrated_database, clock)

    run = store.create_run(
        new_budget_window_run(report, public_api_rows, force=True),
        first_check_at=clock() + FIRST_CHECK,
    )

    assert run.report_id == report.id
    assert run.tax_benefit_model_version_id == public_api_rows.catalog_version_id
    assert run.status is BudgetWindowStatus.RUNNING
    assert run.trigger is RunTrigger.INITIAL
    assert run.force is True
    assert run.max_concurrent_years == 2
    assert run.next_check_at == clock() + FIRST_CHECK
    assert run.completed_at is None
    assert store.get_run(run.id) == run
    assert store.get_run(uuid4()) is None


def test_only_a_budget_window_report_has_budget_window_runs(
    migrated_database, public_api_rows
) -> None:
    baseline, reform = economy_pair(migrated_database, public_api_rows)
    single_year = report_store(migrated_database).create(
        economy_report(baseline, reform)
    )
    store = budget_window_store(migrated_database)

    with pytest.raises(ValueError, match="budget-window report"):
        store.create_run(
            new_budget_window_run(single_year, public_api_rows),
            first_check_at=ManualClock()(),
        )
    unknown = new_budget_window_run(single_year, public_api_rows, report_id=uuid4())
    with pytest.raises(LookupError, match="report"):
        store.create_run(unknown, first_check_at=ManualClock()())


def test_a_run_calculates_at_least_one_year_at_once() -> None:
    with pytest.raises(ValueError, match="max_concurrent_years"):
        NewBudgetWindowRun(
            report_id=uuid4(),
            tax_benefit_model_version_id=uuid4(),
            trigger=RunTrigger.INITIAL,
            max_concurrent_years=0,
        )


def test_each_year_is_one_report_run_of_the_budget_window_run(
    migrated_database, public_api_rows
) -> None:
    report, simulations = budget_window_report(migrated_database, public_api_rows)
    store = budget_window_store(migrated_database)
    run = store.create_run(
        new_budget_window_run(report, public_api_rows),
        first_check_at=ManualClock()(),
    )

    later, first = (
        year_report_run(
            migrated_database,
            public_api_rows,
            (report, simulations),
            year=year,
            budget_window_run_id=run.id,
        )
        for year in (WINDOW_START + 1, WINDOW_START)
    )

    years = store.years(run.id)
    assert [year.report_run.year for year in years] == [WINDOW_START, WINDOW_START + 1]
    assert [year.report_run for year in years] == [first, later]
    assert first.budget_window_run_id == run.id
    assert {year.baseline_run.simulation_id for year in years} == {simulations[0].id}
    assert {year.reform_run.simulation_id for year in years} == {simulations[1].id}
    assert {year.baseline_run.year for year in years} == {
        WINDOW_START,
        WINDOW_START + 1,
    }
    with pytest.raises(IntegrityError, match=ONE_REPORT_RUN_PER_YEAR):
        year_report_run(
            migrated_database,
            public_api_rows,
            (report, simulations),
            year=WINDOW_START,
            budget_window_run_id=run.id,
        )


def test_a_year_outside_the_window_is_refused(
    migrated_database, public_api_rows
) -> None:
    report, simulations = budget_window_report(migrated_database, public_api_rows)
    run = budget_window_store(migrated_database).create_run(
        new_budget_window_run(report, public_api_rows),
        first_check_at=ManualClock()(),
    )

    for year in (WINDOW_START - 1, WINDOW_START + WINDOW_SIZE):
        with pytest.raises(ValueError, match="window"):
            year_report_run(
                migrated_database,
                public_api_rows,
                (report, simulations),
                year=year,
                budget_window_run_id=run.id,
            )


def test_a_budget_window_reports_runs_belong_to_its_budget_window_runs(
    migrated_database, public_api_rows
) -> None:
    report, simulations = budget_window_report(migrated_database, public_api_rows)
    other_report, other_simulations = budget_window_report(
        migrated_database, public_api_rows
    )
    other_run = budget_window_store(migrated_database).create_run(
        new_budget_window_run(other_report, public_api_rows),
        first_check_at=ManualClock()(),
    )

    with pytest.raises(ValueError, match="budget-window run"):
        year_report_run(
            migrated_database,
            public_api_rows,
            (report, simulations),
            year=WINDOW_START,
            budget_window_run_id=None,
        )
    with pytest.raises(IntegrityError, match="fk_report_runs_budget_window_run"):
        year_report_run(
            migrated_database,
            public_api_rows,
            (report, simulations),
            year=WINDOW_START,
            budget_window_run_id=other_run.id,
        )
    del other_simulations


def test_a_single_year_report_run_has_no_budget_window_run(
    migrated_database, public_api_rows
) -> None:
    baseline, reform = economy_pair(migrated_database, public_api_rows)
    single_year = report_store(migrated_database).create(
        economy_report(baseline, reform)
    )
    window_report, _ = budget_window_report(migrated_database, public_api_rows)
    window_run = budget_window_store(migrated_database).create_run(
        new_budget_window_run(window_report, public_api_rows),
        first_check_at=ManualClock()(),
    )

    with pytest.raises(ValueError, match="window"):
        year_report_run(
            migrated_database,
            public_api_rows,
            (single_year, (baseline, reform)),
            year=2026,
            budget_window_run_id=window_run.id,
        )


def test_a_budget_window_run_finishes_once(migrated_database, public_api_rows) -> None:
    report, _ = budget_window_report(migrated_database, public_api_rows)
    clock = ManualClock()
    store = budget_window_store(migrated_database, clock)
    succeeded = store.create_run(
        new_budget_window_run(report, public_api_rows), first_check_at=clock()
    )
    failed = store.create_run(
        new_budget_window_run(report, public_api_rows), first_check_at=clock()
    )
    error = RunError(RunErrorCode.WORKER_ERROR, "The 2027 comparison failed.")

    assert store.finish_run(succeeded.id, BudgetWindowStatus.SUCCEEDED) is True
    assert store.finish_run(failed.id, BudgetWindowStatus.FAILED, error) is True
    assert store.finish_run(failed.id, BudgetWindowStatus.SUCCEEDED) is False

    finished = store.get_run(succeeded.id)
    assert finished is not None
    assert finished.status is BudgetWindowStatus.SUCCEEDED
    assert finished.completed_at == clock()
    assert finished.next_check_at is None
    failure = store.get_run(failed.id)
    assert failure is not None
    assert failure.status is BudgetWindowStatus.FAILED
    assert (failure.error_code, failure.error_message) == (
        "worker_error",
        "The 2027 comparison failed.",
    )
    with pytest.raises(ValueError, match="error"):
        store.finish_run(succeeded.id, BudgetWindowStatus.FAILED)
    with pytest.raises(ValueError, match="finished"):
        store.finish_run(succeeded.id, BudgetWindowStatus.RUNNING)


def test_one_scheduler_claims_a_run_at_a_time(
    migrated_database, public_api_rows
) -> None:
    report, _ = budget_window_report(migrated_database, public_api_rows)
    clock = ManualClock()
    store = budget_window_store(migrated_database, clock)
    run = store.create_run(
        new_budget_window_run(report, public_api_rows), first_check_at=clock()
    )
    lease = timedelta(minutes=2)

    first = store.claim_scheduling(run.id, lease=lease)
    assert first == clock() + lease
    assert store.claim_scheduling(run.id, lease=lease) is None
    store.release_scheduling(
        run.id, claimed_until=first, next_check_at=clock() + FIRST_CHECK
    )
    assert store.claim_scheduling(run.id, lease=lease) is not None
    clock.advance(lease)
    # A scheduler that stopped without releasing its claim loses it.
    assert store.claim_scheduling(run.id, lease=lease) == clock() + lease
    store.finish_run(run.id, BudgetWindowStatus.SUCCEEDED)
    assert store.claim_scheduling(run.id, lease=lease) is None


def test_a_late_release_leaves_a_newer_claim_in_place(
    migrated_database, public_api_rows
) -> None:
    report, _ = budget_window_report(migrated_database, public_api_rows)
    clock = ManualClock()
    store = budget_window_store(migrated_database, clock)
    run = store.create_run(
        new_budget_window_run(report, public_api_rows), first_check_at=clock()
    )
    lease = timedelta(minutes=2)
    expired = store.claim_scheduling(run.id, lease=lease)
    clock.advance(lease)
    current = store.claim_scheduling(run.id, lease=lease)
    assert expired is not None
    assert current is not None

    # The first scheduler finishes after its lease ended.
    store.release_scheduling(
        run.id, claimed_until=expired, next_check_at=clock() + FIRST_CHECK
    )

    held = store.get_run(run.id)
    assert held is not None
    assert held.scheduling_claimed_until == current
    assert store.claim_scheduling(run.id, lease=lease) is None


SIMULTANEOUS_SCHEDULERS = 6


def test_simultaneous_schedulers_claim_a_run_once(
    migrated_database, public_api_rows
) -> None:
    report, _ = budget_window_report(migrated_database, public_api_rows)
    store = budget_window_store(migrated_database)
    run = store.create_run(
        new_budget_window_run(report, public_api_rows),
        first_check_at=ManualClock()(),
    )
    all_ready = Barrier(SIMULTANEOUS_SCHEDULERS)

    def claim(_: int) -> bool:
        all_ready.wait()
        return store.claim_scheduling(run.id, lease=timedelta(minutes=2)) is not None

    with ThreadPoolExecutor(max_workers=SIMULTANEOUS_SCHEDULERS) as pool:
        claims = list(pool.map(claim, range(SIMULTANEOUS_SCHEDULERS)))

    assert sum(claims) == 1


def test_releasing_a_claim_sets_the_next_check(
    migrated_database, public_api_rows
) -> None:
    report, _ = budget_window_report(migrated_database, public_api_rows)
    clock = ManualClock()
    store = budget_window_store(migrated_database, clock)
    run = store.create_run(
        new_budget_window_run(report, public_api_rows), first_check_at=clock()
    )
    claimed_until = store.claim_scheduling(run.id, lease=timedelta(minutes=2))
    assert claimed_until is not None

    store.release_scheduling(
        run.id, claimed_until=claimed_until, next_check_at=clock() + FIRST_CHECK
    )

    released = store.get_run(run.id)
    assert released is not None
    assert released.next_check_at == clock() + FIRST_CHECK
    assert released.scheduling_claimed_until is None


def test_due_runs_are_the_running_runs_whose_check_is_due(
    migrated_database, public_api_rows
) -> None:
    report, _ = budget_window_report(migrated_database, public_api_rows)
    clock = ManualClock()
    store = budget_window_store(migrated_database, clock)
    later, due, finished = (
        store.create_run(
            new_budget_window_run(report, public_api_rows),
            first_check_at=clock() + offset,
        )
        for offset in (FIRST_CHECK, timedelta(0), timedelta(0))
    )
    store.finish_run(finished.id, BudgetWindowStatus.SUCCEEDED)

    assert [run.id for run in store.due_runs(now=clock(), limit=10)] == [due.id]
    clock.advance(FIRST_CHECK)
    assert [run.id for run in store.due_runs(now=clock(), limit=10)] == [
        due.id,
        later.id,
    ]
    assert len(store.due_runs(now=clock(), limit=1)) == 1


def test_a_reports_budget_window_runs_are_listed_newest_first(
    migrated_database, public_api_rows
) -> None:
    report, _ = budget_window_report(migrated_database, public_api_rows)
    clock = ManualClock()
    store = budget_window_store(migrated_database, clock)
    first = store.create_run(
        new_budget_window_run(report, public_api_rows), first_check_at=clock()
    )
    clock.advance(timedelta(minutes=1))
    rerun = store.create_run(
        new_budget_window_run(report, public_api_rows, trigger=RunTrigger.MANUAL),
        first_check_at=clock(),
    )

    assert store.runs(report.id) == [rerun, first]
    assert store.latest_run(report.id) == rerun
    assert store.latest_run(uuid4()) is None


SIMULTANEOUS_FIRST_OPENS = 6


def test_simultaneous_first_runs_of_a_report_add_one(
    migrated_database, public_api_rows
) -> None:
    report, _ = budget_window_report(migrated_database, public_api_rows)
    store = budget_window_store(migrated_database)
    first_open = new_budget_window_run(
        report, public_api_rows, trigger=RunTrigger.SYSTEM
    )
    all_ready = Barrier(SIMULTANEOUS_FIRST_OPENS)

    def open_first_time(_: int):
        all_ready.wait()
        return store.create_first_run(first_open, first_check_at=ManualClock()())

    with ThreadPoolExecutor(max_workers=SIMULTANEOUS_FIRST_OPENS) as pool:
        started = list(pool.map(open_first_time, range(SIMULTANEOUS_FIRST_OPENS)))

    assert sum(start.created for start in started) == 1
    assert {start.run.id for start in started} == {started[0].run.id}
    assert len(store.runs(report.id)) == 1
    again = store.create_first_run(first_open, first_check_at=ManualClock()())
    assert again.created is False


def test_each_years_budget_figures_are_read_back_in_year_order(
    migrated_database, public_api_rows
) -> None:
    report, simulations = budget_window_report(migrated_database, public_api_rows)
    store = budget_window_store(migrated_database)
    run = store.create_run(
        new_budget_window_run(report, public_api_rows),
        first_check_at=ManualClock()(),
    )
    for year in (WINDOW_START + 1, WINDOW_START):
        report_run = year_report_run(
            migrated_database,
            public_api_rows,
            (report, simulations),
            year=year,
            budget_window_run_id=run.id,
        )
        write_year_budget(migrated_database, report_run, scale=year - 2025)

    assert store.year_budgets(run.id) == [
        YearBudget(
            year=WINDOW_START,
            budgetary_impact=-1.5,
            tax_revenue_impact=2.25,
            state_tax_revenue_impact=0.25,
            benefit_spending_impact=3.75,
        ),
        YearBudget(
            year=WINDOW_START + 1,
            budgetary_impact=-3.0,
            tax_revenue_impact=4.5,
            state_tax_revenue_impact=0.5,
            benefit_spending_impact=7.5,
        ),
    ]


def test_a_years_simulation_runs_are_read_with_its_report_run(
    migrated_database, public_api_rows
) -> None:
    report, simulations = budget_window_report(migrated_database, public_api_rows)
    store = budget_window_store(migrated_database)
    run = store.create_run(
        new_budget_window_run(report, public_api_rows),
        first_check_at=ManualClock()(),
    )
    report_run = year_report_run(
        migrated_database,
        public_api_rows,
        (report, simulations),
        year=WINDOW_START,
        budget_window_run_id=run.id,
    )
    set_simulation_run_status(
        migrated_database, report_run.reform_simulation_run_id, "failed"
    )

    (year,) = store.years(run.id)

    assert year.baseline_run.status is RunStatus.PENDING
    assert year.reform_run.status is RunStatus.FAILED
    assert year.reform_run.error_code == "test_failure"


def test_the_runtime_role_updates_budget_window_runs_but_cannot_delete_them(
    migrated_database, public_api_rows
) -> None:
    report, _ = budget_window_report(migrated_database, public_api_rows)
    budget_window_store(migrated_database).create_run(
        new_budget_window_run(report, public_api_rows),
        first_check_at=ManualClock()(),
    )
    engine = create_simulation_engine(migrated_database.runtime_url)
    try:
        with engine.begin() as connection:
            updated = connection.execute(
                text("UPDATE simulation.budget_window_runs SET updated_at = now()")
            )
        assert updated.rowcount == 1
        with (
            pytest.raises(ProgrammingError, match="permission denied"),
            engine.begin() as connection,
        ):
            connection.execute(text("DELETE FROM simulation.budget_window_runs"))
    finally:
        engine.dispose()
