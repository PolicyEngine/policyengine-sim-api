"""The restart check's reads and writes for runs whose Modal call never started.

These run against a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``), as the Modal worker role the restart
check connects as, each in a transaction rolled back afterwards. The rows are
inserted first, as the superuser, with the simulation service's own writes.
Every row a test inserts shares the transaction's start time as its
``created_at``, so each listing names its window around that time.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from conftest import act_as_the_modal_worker, rolled_back_transaction
from economy_rows import (
    CURRENT_LAW,
    VERSIONS,
    EconomyCatalog,
    insert_economy_catalog,
    insert_reusable_run,
)
from policyengine_simulation_persistence import (
    BudgetWindowYear,
    HouseholdReportSimulation,
    IdleBudgetWindow,
    NewBudgetWindowReport,
    NewEconomyReport,
    NewEconomySimulation,
    NewHouseholdJob,
    NewHouseholdReport,
    ReportRunTrigger,
    ReportSimulation,
    RestartKind,
    RunCall,
    RunRestartStore,
    SimulationChoice,
    SimulationType,
    StoredHouseholdReportRun,
    StoredHouseholdReportSide,
    StoredRunUnreadableError,
    UnstartedRun,
    WaitingYear,
    budget_window_runs,
    insert_budget_window_report,
    insert_economy_report,
    insert_economy_simulation,
    insert_household_job,
    insert_household_report,
    list_idle_budget_windows,
    list_unstarted_runs,
    mark_restart,
    mark_waiting_year,
    read_earliest_waiting_year,
    read_report_run_simulation_run_ids,
    read_run_call,
    read_unstarted_household_report_run,
    read_unstarted_report_run,
    read_unstarted_simulation_run,
    record_household_call,
    record_report_run_call,
    report_runs,
    simulation_runs,
    simulations,
)
from public_api_rows import (
    StoredValue,
    insert_catalog_version,
    insert_dynamic,
    insert_household,
    insert_policy,
)
from sqlalchemy import func, insert, select, text, update

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

START = datetime(2026, 1, 1, tzinfo=UTC)
CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
ELASTICITY = "gov.simulation.labor_supply_responses.elasticities.income"
REPORT_OPTIONS = {"include_cliffs": True, "spm": {"geography_kind": "national"}}
HOUSEHOLD_DOCUMENT = {
    "people": [
        {
            "id": "you",
            "values": {"age": 40},
            "memberships": {"household": "your household"},
        }
    ],
    "household": [{"id": "your household", "values": {}}],
}
LISTING_LIMIT = 100


def _transaction_start(connection: Connection) -> datetime:
    """Return the time every row the test inserts records as its creation."""
    return connection.execute(select(func.now())).scalar_one()


def _listed(
    connection: Connection, *, around: datetime, limit: int = LISTING_LIMIT
) -> tuple[UnstartedRun, ...]:
    """List the unstarted runs created within a second of ``around``."""
    return list_unstarted_runs(
        connection,
        created_after=around - timedelta(seconds=1),
        created_before=around + timedelta(seconds=1),
        limit=limit,
    )


def _new_side(choice: SimulationChoice = CURRENT_LAW) -> ReportSimulation:
    return ReportSimulation(
        simulation_id=uuid4(), simulation_run_id=uuid4(), choice=choice, reused=False
    )


def _new_report(
    economy: EconomyCatalog,
    *,
    baseline: ReportSimulation | None = None,
    reform: ReportSimulation | None = None,
) -> NewEconomyReport:
    return NewEconomyReport(
        id=uuid4(),
        run_id=uuid4(),
        country=economy.catalog.country,
        label="Child tax credit",
        description=None,
        tax_benefit_model_id=economy.catalog.model_id,
        population=economy.population(),
        versions=VERSIONS,
        inputs=REPORT_OPTIONS,
        baseline=baseline or _new_side(),
        reform=reform or _new_side(),
    )


def _new_simulation(
    economy: EconomyCatalog, choice: SimulationChoice = CURRENT_LAW
) -> NewEconomySimulation:
    return NewEconomySimulation(
        id=uuid4(),
        run_id=uuid4(),
        choice=choice,
        population=economy.population(),
        versions=VERSIONS,
    )


def _insert_household_job(connection: Connection, economy: EconomyCatalog) -> UUID:
    household_id = insert_household(
        connection, country="us", default_year=2026, document=HOUSEHOLD_DOCUMENT
    )
    job_id = uuid4()
    insert_household_job(
        connection,
        NewHouseholdJob(
            id=job_id,
            country="us",
            household_id=household_id,
            policy_id=None,
            year=2026,
            request_data={"country": "us", "household_id": str(household_id)},
            tax_benefit_model_version_id=economy.catalog.version_id,
            country_package_version=VERSIONS.country_package_version,
            policyengine_version=VERSIONS.policyengine_version,
        ),
    )
    return job_id


@pytest.fixture
def connection(public_api_database_url) -> Iterator[Connection]:
    """Yield the superuser's connection in a transaction rolled back afterwards."""
    with rolled_back_transaction(public_api_database_url) as superuser_connection:
        yield superuser_connection


def test_an_unstarted_run_of_each_kind_is_listed_and_a_reports_sides_are_not(
    connection,
) -> None:
    economy = insert_economy_catalog(connection)
    report = _new_report(economy)
    insert_economy_report(connection, report)
    simulation = _new_simulation(economy)
    insert_economy_simulation(connection, simulation)
    job_id = _insert_household_job(connection, economy)
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)

    listed = _listed(connection, around=created_at)

    assert {(run.kind, run.run_id) for run in listed} == {
        (RestartKind.REPORT_RUN, report.run_id),
        (RestartKind.SIMULATION_RUN, simulation.run_id),
        (RestartKind.HOUSEHOLD_JOB, job_id),
    }
    assert {run.created_at for run in listed} == {created_at}


def test_a_run_created_outside_the_window_is_not_listed(connection) -> None:
    economy = insert_economy_catalog(connection)
    insert_economy_simulation(connection, _new_simulation(economy))
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)

    too_new = list_unstarted_runs(
        connection,
        created_after=created_at - timedelta(minutes=10),
        created_before=created_at - timedelta(minutes=3),
        limit=LISTING_LIMIT,
    )
    too_old = list_unstarted_runs(
        connection,
        created_after=created_at + timedelta(minutes=3),
        created_before=created_at + timedelta(minutes=10),
        limit=LISTING_LIMIT,
    )

    assert too_new == ()
    assert too_old == ()


def test_the_listing_takes_the_oldest_runs_up_to_its_limit(connection) -> None:
    economy = insert_economy_catalog(connection)
    for _ in range(3):
        insert_economy_simulation(connection, _new_simulation(economy))
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)

    assert len(_listed(connection, around=created_at, limit=2)) == 2


def test_a_run_with_a_call_or_no_longer_pending_is_not_listed(connection) -> None:
    economy = insert_economy_catalog(connection)
    called = _new_report(economy)
    insert_economy_report(connection, called)
    record_report_run_call(
        connection, report_run_id=called.run_id, modal_call_id="fc-original"
    )
    claimed = _new_simulation(economy)
    insert_economy_simulation(connection, claimed)
    connection.execute(
        update(simulation_runs)
        .where(simulation_runs.c.id == claimed.run_id)
        .values(status="running", started_at=func.now())
    )
    job_id = _insert_household_job(connection, economy)
    record_household_call(connection, job_id=job_id, modal_call_id="fc-household")
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)

    listed = _listed(connection, around=created_at)

    # The called report run's sides stay named by a pending report run.
    assert listed == ()


def test_a_marked_run_is_marked_once_and_never_listed_again(connection) -> None:
    economy = insert_economy_catalog(connection)
    simulation = _new_simulation(economy)
    insert_economy_simulation(connection, simulation)
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)
    (unstarted,) = _listed(connection, around=created_at)

    first = mark_restart(connection, unstarted)
    second = mark_restart(connection, unstarted)

    assert (first, second) == (True, False)
    assert _listed(connection, around=created_at) == ()
    row = connection.execute(
        select(simulation_runs.c.status, simulation_runs.c.modal_call_id).where(
            simulation_runs.c.id == simulation.run_id
        )
    ).one()
    assert (row.status, row.modal_call_id) == ("pending", None)


def test_a_run_given_a_call_after_it_was_listed_is_not_marked(connection) -> None:
    economy = insert_economy_catalog(connection)
    report = _new_report(economy)
    insert_economy_report(connection, report)
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)
    (unstarted,) = _listed(connection, around=created_at)
    record_report_run_call(
        connection, report_run_id=report.run_id, modal_call_id="fc-late"
    )

    assert mark_restart(connection, unstarted) is False


def test_each_kind_is_marked_in_its_own_table(connection) -> None:
    economy = insert_economy_catalog(connection)
    report = _new_report(economy)
    insert_economy_report(connection, report)
    _insert_household_job(connection, economy)
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)

    marked = {
        run.kind: mark_restart(connection, run)
        for run in _listed(connection, around=created_at)
    }

    assert marked == {RestartKind.REPORT_RUN: True, RestartKind.HOUSEHOLD_JOB: True}
    assert _listed(connection, around=created_at) == ()


def test_a_budget_window_year_is_not_listed(connection) -> None:
    economy = insert_economy_catalog(connection)
    report = _new_report(economy)
    insert_economy_report(connection, report)
    window_run_id = uuid4()
    connection.execute(
        insert(budget_window_runs).values(
            id=window_run_id,
            report_id=report.id,
            tax_benefit_model_version_id=economy.catalog.version_id,
            trigger=ReportRunTrigger.INITIAL,
            recalculate=False,
            status="pending",
        )
    )
    connection.execute(
        update(report_runs)
        .where(report_runs.c.id == report.run_id)
        .values(budget_window_run_id=window_run_id, year=2026)
    )
    # The update above changes the year's updated_at; put it back to its
    # creation, as a year the service creates with its window stays.
    connection.execute(
        update(report_runs)
        .where(report_runs.c.id == report.run_id)
        .values(updated_at=report_runs.c.created_at)
    )
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)

    assert _listed(connection, around=created_at) == ()


def _new_household_report(
    economy: EconomyCatalog,
    household_id: UUID,
    *,
    reform_policy_id: UUID | None = None,
    with_reform: bool = True,
) -> NewHouseholdReport:
    reform = (
        HouseholdReportSimulation(
            simulation_id=uuid4(),
            simulation_run_id=uuid4(),
            policy_id=reform_policy_id,
        )
        if with_reform
        else None
    )
    return NewHouseholdReport(
        id=uuid4(),
        run_id=uuid4(),
        country="us",
        label="A larger child tax credit for one family",
        description=None,
        tax_benefit_model_id=economy.catalog.model_id,
        tax_benefit_model_version_id=economy.catalog.version_id,
        household_id=household_id,
        year=2027,
        versions=VERSIONS,
        inputs={},
        baseline=HouseholdReportSimulation(
            simulation_id=uuid4(), simulation_run_id=uuid4(), policy_id=None
        ),
        reform=reform,
    )


def _stored_household(connection: Connection) -> UUID:
    return insert_household(
        connection, country="us", default_year=2026, document=HOUSEHOLD_DOCUMENT
    )


def test_a_household_report_run_is_listed_as_its_own_kind_and_its_sides_are_not(
    connection,
) -> None:
    """Its one household function call claims both simulation runs."""
    economy = insert_economy_catalog(connection)
    report = _new_household_report(economy, _stored_household(connection))
    insert_household_report(connection, report)
    simulation = _new_simulation(economy)
    insert_economy_simulation(connection, simulation)
    # A household simulation's run that no report names: there are none
    # yet, and the check couldn't rebuild its call.
    connection.execute(
        update(simulations)
        .where(simulations.c.id == simulation.id)
        .values(
            simulation_type=SimulationType.HOUSEHOLD,
            dataset_id=None,
            household_id=report.household_id,
        )
    )
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)

    assert _listed(connection, around=created_at) == (
        UnstartedRun(
            kind=RestartKind.HOUSEHOLD_REPORT_RUN,
            run_id=report.run_id,
            created_at=created_at,
        ),
    )


def test_a_household_report_runs_stored_rows_are_read_back(connection) -> None:
    economy = insert_economy_catalog(connection, parameter_names=(CTC_AMOUNT,))
    policy_id = insert_policy(
        connection, economy.catalog, [StoredValue(CTC_AMOUNT, 3_000, START)]
    )
    report = _new_household_report(
        economy, _stored_household(connection), reform_policy_id=policy_id
    )
    insert_household_report(connection, report)
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)

    stored = read_unstarted_household_report_run(connection, report.run_id)

    assert stored == StoredHouseholdReportRun(
        report_run_id=report.run_id,
        country="us",
        versions=VERSIONS,
        household_id=report.household_id,
        year=2027,
        catalog_version_id=economy.catalog.version_id,
        baseline=StoredHouseholdReportSide(
            simulation_run_id=report.baseline.simulation_run_id, policy_id=None
        ),
        reform=StoredHouseholdReportSide(
            simulation_run_id=report.reform.simulation_run_id, policy_id=policy_id
        ),
        created_at=created_at,
    )
    assert read_unstarted_household_report_run(connection, uuid4()) is None


def test_a_household_report_run_of_the_baseline_alone_reads_back_no_reform(
    connection,
) -> None:
    economy = insert_economy_catalog(connection)
    report = _new_household_report(
        economy, _stored_household(connection), with_reform=False
    )
    insert_household_report(connection, report)
    act_as_the_modal_worker(connection)

    stored = read_unstarted_household_report_run(connection, report.run_id)

    assert stored is not None
    assert stored.reform is None
    assert stored.baseline.simulation_run_id == report.baseline.simulation_run_id


def test_a_household_report_run_whose_sides_differ_cannot_be_read(
    connection,
) -> None:
    economy = insert_economy_catalog(connection)
    report = _new_household_report(economy, _stored_household(connection))
    insert_household_report(connection, report)
    other_catalog = insert_catalog_version(
        connection,
        country="us",
        model_id=economy.catalog.model_id,
        policyengine_version="9.9.9",
    )
    connection.execute(
        update(simulation_runs)
        .where(simulation_runs.c.id == report.reform.simulation_run_id)
        .values(tax_benefit_model_version_id=other_catalog.version_id)
    )
    act_as_the_modal_worker(connection)

    with pytest.raises(StoredRunUnreadableError, match="catalog versions"):
        read_unstarted_household_report_run(connection, report.run_id)


def test_a_report_runs_stored_inputs_are_read_back(connection) -> None:
    economy = insert_economy_catalog(
        connection, parameter_names=(CTC_AMOUNT, ELASTICITY)
    )
    policy_id = insert_policy(
        connection, economy.catalog, [StoredValue(CTC_AMOUNT, 3_000, START)]
    )
    dynamic_id = insert_dynamic(
        connection, economy.catalog, [StoredValue(ELASTICITY, -0.05, START)]
    )
    reform_choice = SimulationChoice(policy_id=policy_id, dynamic_id=dynamic_id)
    reused = insert_reusable_run(connection, economy)
    report = _new_report(
        economy,
        baseline=ReportSimulation(
            simulation_id=reused.simulation_id,
            simulation_run_id=reused.simulation_run_id,
            choice=CURRENT_LAW,
            reused=True,
        ),
        reform=_new_side(reform_choice),
    )
    insert_economy_report(connection, report)
    act_as_the_modal_worker(connection)

    stored = read_unstarted_report_run(connection, report.run_id)

    assert stored is not None
    assert stored.report_run_id == report.run_id
    assert stored.country == "us"
    assert stored.versions == VERSIONS
    assert stored.options == REPORT_OPTIONS
    assert stored.scope.catalog_version_id == economy.catalog.version_id
    assert stored.scope.region_code == "us"
    assert stored.scope.dataset_name == "populace_us_2024"
    assert stored.scope.year == 2026
    assert stored.baseline.simulation_run_id == reused.simulation_run_id
    assert stored.baseline.choice == CURRENT_LAW
    assert stored.baseline.reform == {}
    assert stored.reform.simulation_run_id == report.reform.simulation_run_id
    assert stored.reform.choice == reform_choice
    assert stored.reform.reform == {
        CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000},
        ELASTICITY: {"2026-01-01.2200-12-31": -0.05},
    }
    assert stored.created_at == _transaction_start(connection)


def test_a_standalone_runs_stored_inputs_are_read_back(connection) -> None:
    economy = insert_economy_catalog(connection, parameter_names=(CTC_AMOUNT,))
    policy_id = insert_policy(
        connection, economy.catalog, [StoredValue(CTC_AMOUNT, 2_500, START)]
    )
    choice = SimulationChoice(policy_id=policy_id, dynamic_id=None)
    simulation = _new_simulation(economy, choice)
    insert_economy_simulation(connection, simulation)
    act_as_the_modal_worker(connection)

    stored = read_unstarted_simulation_run(connection, simulation.run_id)

    assert stored is not None
    assert stored.country == "us"
    assert stored.versions == VERSIONS
    assert stored.scope.catalog_version_id == economy.catalog.version_id
    assert (stored.scope.region_code, stored.scope.dataset_name) == (
        "us",
        "populace_us_2024",
    )
    assert stored.scope.year == 2026
    assert stored.run.simulation_run_id == simulation.run_id
    assert stored.run.choice == choice
    assert stored.run.reform == {CTC_AMOUNT: {"2026-01-01.2200-12-31": 2_500}}
    assert stored.created_at == _transaction_start(connection)


def test_a_missing_run_reads_as_none(connection) -> None:
    act_as_the_modal_worker(connection)

    assert read_unstarted_report_run(connection, uuid4()) is None
    assert read_unstarted_simulation_run(connection, uuid4()) is None


def test_a_run_whose_region_is_gone_cannot_be_read(connection) -> None:
    economy = insert_economy_catalog(connection)
    simulation = _new_simulation(economy)
    insert_economy_simulation(connection, simulation)
    connection.execute(
        update(simulation_runs)
        .where(simulation_runs.c.id == simulation.run_id)
        .values(region_id=None)
    )
    act_as_the_modal_worker(connection)

    with pytest.raises(StoredRunUnreadableError, match="region"):
        read_unstarted_simulation_run(connection, simulation.run_id)


def test_a_report_run_whose_sides_differ_cannot_be_read(connection) -> None:
    economy = insert_economy_catalog(connection)
    report = _new_report(economy)
    insert_economy_report(connection, report)
    connection.execute(
        update(simulation_runs)
        .where(simulation_runs.c.id == report.reform.simulation_run_id)
        .values(year=2027)
    )
    act_as_the_modal_worker(connection)

    with pytest.raises(StoredRunUnreadableError, match="sides"):
        read_unstarted_report_run(connection, report.run_id)


def test_a_report_runs_simulation_runs_are_read_even_when_its_rows_are_not(
    connection,
) -> None:
    """A report run that can't be rebuilt is failed with its new simulation runs."""
    economy = insert_economy_catalog(connection)
    report = _new_report(economy)
    insert_economy_report(connection, report)
    connection.execute(
        update(simulation_runs)
        .where(simulation_runs.c.id == report.reform.simulation_run_id)
        .values(region_id=None)
    )
    act_as_the_modal_worker(connection)

    with pytest.raises(StoredRunUnreadableError):
        read_unstarted_report_run(connection, report.run_id)
    assert read_report_run_simulation_run_ids(connection, report.run_id) == (
        report.baseline.simulation_run_id,
        report.reform.simulation_run_id,
    )
    assert read_report_run_simulation_run_ids(connection, uuid4()) == ()


def test_a_rows_status_and_recorded_call_are_read_back(connection) -> None:
    economy = insert_economy_catalog(connection)
    report = _new_report(economy)
    insert_economy_report(connection, report)
    record_report_run_call(
        connection, report_run_id=report.run_id, modal_call_id="fc-restarted"
    )
    job_id = _insert_household_job(connection, economy)
    act_as_the_modal_worker(connection)

    report_run = UnstartedRun(
        kind=RestartKind.REPORT_RUN, run_id=report.run_id, created_at=START
    )
    job = UnstartedRun(kind=RestartKind.HOUSEHOLD_JOB, run_id=job_id, created_at=START)
    missing = UnstartedRun(
        kind=RestartKind.SIMULATION_RUN, run_id=uuid4(), created_at=START
    )

    assert read_run_call(connection, report_run) == RunCall(
        status="pending", modal_call_id="fc-restarted"
    )
    assert read_run_call(connection, job) == RunCall(
        status="pending", modal_call_id=None
    )
    assert read_run_call(connection, missing) is None


def _new_window(economy: EconomyCatalog, years: int = 3) -> NewBudgetWindowReport:
    return NewBudgetWindowReport(
        id=uuid4(),
        window_run_id=uuid4(),
        country="us",
        label="Three-year budget window",
        description=None,
        tax_benefit_model_id=economy.catalog.model_id,
        population=economy.population(year=2026),
        versions=VERSIONS,
        inputs=REPORT_OPTIONS,
        years=tuple(
            BudgetWindowYear(
                year=2026 + offset,
                report_run_id=uuid4(),
                baseline=_new_side(),
                reform=_new_side(),
            )
            for offset in range(years)
        ),
    )


def _idle_windows(
    connection: Connection, *, around: datetime
) -> tuple[IdleBudgetWindow, ...]:
    """List the idle windows whose last change is within a second of ``around``."""
    return list_idle_budget_windows(
        connection,
        idle_after=around - timedelta(seconds=1),
        idle_before=around + timedelta(seconds=1),
        limit=LISTING_LIMIT,
    )


def test_an_idle_window_with_years_waiting_is_listed_for_a_restart(
    connection,
) -> None:
    """A window whose first years the service never started waits for nothing."""
    economy = insert_economy_catalog(connection)
    window = _new_window(economy)
    insert_budget_window_report(connection, window)
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)

    assert _idle_windows(connection, around=created_at) == (
        IdleBudgetWindow(run_id=window.window_run_id, last_changed_at=created_at),
    )
    # Its years are never listed as report runs of their own.
    assert _listed(connection, around=created_at) == ()


def test_a_window_with_a_year_under_way_or_finished_is_not_listed(
    connection,
) -> None:
    economy = insert_economy_catalog(connection)
    started = _new_window(economy)
    insert_budget_window_report(connection, started)
    record_report_run_call(
        connection,
        report_run_id=started.years[0].report_run_id,
        modal_call_id="fc-year",
    )
    finished = _new_window(economy)
    insert_budget_window_report(connection, finished)
    connection.execute(
        update(budget_window_runs)
        .where(budget_window_runs.c.id == finished.window_run_id)
        .values(status="failed", completed_at=func.now())
    )
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)

    assert _idle_windows(connection, around=created_at) == ()


def test_a_window_whose_earliest_waiting_year_was_marked_is_not_listed(
    connection,
) -> None:
    """A window is restarted at most once for each time it stops."""
    economy = insert_economy_catalog(connection)
    window = _new_window(economy)
    insert_budget_window_report(connection, window)
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)
    first_year = WaitingYear(report_run_id=window.years[0].report_run_id, year=2026)

    assert mark_waiting_year(connection, first_year) is True
    assert mark_waiting_year(connection, first_year) is False
    marked_at = connection.execute(
        select(report_runs.c.updated_at).where(
            report_runs.c.id == first_year.report_run_id
        )
    ).scalar_one()
    assert _idle_windows(connection, around=created_at) == ()
    assert _idle_windows(connection, around=marked_at) == ()


def test_a_window_idle_outside_the_listings_bounds_is_not_listed(
    connection,
) -> None:
    economy = insert_economy_catalog(connection)
    insert_budget_window_report(connection, _new_window(economy))
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)

    too_recent = list_idle_budget_windows(
        connection,
        idle_after=created_at - timedelta(minutes=10),
        idle_before=created_at - timedelta(minutes=3),
        limit=LISTING_LIMIT,
    )

    assert too_recent == ()


def test_the_earliest_unmarked_waiting_year_of_a_window_is_read_back(
    connection,
) -> None:
    economy = insert_economy_catalog(connection)
    window = _new_window(economy)
    insert_budget_window_report(connection, window)
    record_report_run_call(
        connection,
        report_run_id=window.years[0].report_run_id,
        modal_call_id="fc-first-year",
    )
    act_as_the_modal_worker(connection)

    assert read_earliest_waiting_year(connection, window.window_run_id) == (
        WaitingYear(report_run_id=window.years[1].report_run_id, year=2027)
    )
    assert read_earliest_waiting_year(connection, uuid4()) is None


@pytest.fixture
def savepoint_store(connection) -> RunRestartStore:
    """Return a store whose every transaction is a savepoint of ``connection``."""

    @contextmanager
    def transaction() -> Iterator[Connection]:
        with connection.begin_nested():
            yield connection

    return RunRestartStore(transaction)


def test_the_store_lists_marks_and_reads_in_transactions_of_its_own(
    connection, savepoint_store
) -> None:
    economy = insert_economy_catalog(connection)
    report = _new_report(economy)
    insert_economy_report(connection, report)
    simulation = _new_simulation(economy)
    insert_economy_simulation(connection, simulation)
    window = _new_window(economy)
    insert_budget_window_report(connection, window)
    household_report = _new_household_report(economy, _stored_household(connection))
    insert_household_report(connection, household_report)
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)

    listed = savepoint_store.list_unstarted(
        created_after=created_at - timedelta(seconds=1),
        created_before=created_at + timedelta(seconds=1),
        limit=LISTING_LIMIT,
    )
    by_kind = {run.kind: run for run in listed}
    report_run = savepoint_store.read_report_run(report.run_id)
    simulation_run = savepoint_store.read_simulation_run(simulation.run_id)
    household_report_run = savepoint_store.read_household_report_run(
        household_report.run_id
    )
    marked = savepoint_store.mark_restart(by_kind[RestartKind.REPORT_RUN])
    call = savepoint_store.read_call(by_kind[RestartKind.SIMULATION_RUN])
    windows = savepoint_store.list_idle_windows(
        idle_after=created_at - timedelta(seconds=1),
        idle_before=created_at + timedelta(seconds=1),
        limit=LISTING_LIMIT,
    )
    waiting = savepoint_store.read_earliest_waiting_year(window.window_run_id)
    year_marked = waiting is not None and savepoint_store.mark_waiting_year(waiting)
    sides = savepoint_store.read_report_run_simulation_run_ids(report.run_id)

    assert set(by_kind) == {
        RestartKind.REPORT_RUN,
        RestartKind.SIMULATION_RUN,
        RestartKind.HOUSEHOLD_REPORT_RUN,
    }
    assert household_report_run is not None
    assert household_report_run.report_run_id == household_report.run_id
    assert call == RunCall(status="pending", modal_call_id=None)
    assert [run.run_id for run in windows] == [window.window_run_id]
    assert waiting == WaitingYear(
        report_run_id=window.years[0].report_run_id, year=2026
    )
    assert year_marked is True
    assert sides == (
        report.baseline.simulation_run_id,
        report.reform.simulation_run_id,
    )
    assert report_run is not None
    assert report_run.report_run_id == report.run_id
    assert simulation_run is not None
    assert simulation_run.run.simulation_run_id == simulation.run_id
    assert marked is True


def test_the_restart_writes_need_only_the_modal_worker_roles_privileges(
    connection,
) -> None:
    economy = insert_economy_catalog(connection)
    report = _new_report(economy)
    insert_economy_report(connection, report)
    created_at = _transaction_start(connection)
    act_as_the_modal_worker(connection)
    (unstarted,) = _listed(connection, around=created_at)

    assert mark_restart(connection, unstarted) is True
    assert (
        record_report_run_call(
            connection, report_run_id=report.run_id, modal_call_id="fc-restarted"
        )
        == "pending"
    )
    assert connection.execute(text("SELECT current_user")).scalar_one() == (
        "policyengine_v2_modal_worker"
    )
