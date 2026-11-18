"""Economy reports, simulations, re-runs and windows on ORM sessions.

The economy code runs its statements through ORM sessions on the mapped
classes. Find-or-create inserts, conditional updates and re-run locks are single
statements whose meaning depends on the database, so these tests run them on
real sessions: at once from two transactions, through a lost connection, and
past the statement limit.

They use a database API v1's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``). A test of the store's transactions commits
a catalog of its own and deletes it after; the others work in one transaction
rolled back afterwards.
"""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from committed_rows import (
    LOCK_WAIT_TIMEOUT_SECONDS,
    run_together,
    wait_until_another_backend_waits_for,
)
from conftest import (
    act_as_the_service,
    in_session,
    rolled_back_transaction,
    session_in,
)
from economy_rows import (
    CURRENT_LAW,
    POLICYENGINE_VERSION,
    VERSIONS,
    ReportRunOutcome,
    RunShape,
    StoredRun,
    insert_economy_catalog,
    insert_pending_report,
    insert_report_of,
    insert_report_run_of,
    insert_simulation_run,
)
from policyengine_simulation_persistence import (
    BudgetWindowRun,
    EconomyRecordOutcomeUnknownError,
    EconomyRecordReferenceError,
    EconomyReportDefinition,
    EconomyRequest,
    EconomyRunStore,
    EconomySimulationDefinition,
    KeyedBudgetWindow,
    KeyedEconomyReport,
    KeyedEconomySimulation,
    KeyedWindowYear,
    NewReportRerun,
    NewSimulationRerun,
    NewWindowRerun,
    ProposedSide,
    Report,
    ReportRun,
    ReportRunStatus,
    RerunRecordDeletedError,
    RerunSide,
    RunVersions,
    SharedRowNotSettledError,
    Simulation,
    SimulationChoice,
    SimulationRun,
    SimulationStatus,
    WindowRerunYear,
    create_session_factory,
    economy_inputs,
    economy_rerun_records,
    economy_run_reads,
    economy_run_records,
    fail_unstarted_report_run,
    fail_unstarted_simulation_run,
    find_or_create_budget_window,
    find_or_create_economy_report,
    find_or_create_economy_simulation,
    insert_report_rerun,
    insert_simulation_rerun,
    insert_window_rerun,
    keyed_economy_records,
    keyed_window_records,
    policy_reform,
    read_economy_report,
    read_economy_simulation,
    read_report_definition,
    read_simulation_definition,
    record_report_run_call,
    record_simulation_run_call,
    rerun_definitions,
    shared_rows,
)
from policyengine_simulation_persistence.economy_run_records import (
    references_checked,
)
from policyengine_simulation_persistence.keyed_window_records import (
    WindowYearToInsert,
    insert_window_years,
    window_run_row,
)
from policyengine_simulation_persistence.shared_rows import find_or_insert_window_run
from public_api_rows import StoredValue, insert_dynamic, insert_policy
from session_probes import (
    end_connection_at,
    end_connection_before_statement,
    statements_naming_no_mapped_class,
    statements_sent,
)
from sqlalchemy import ForeignKeyConstraint, event, func, select, text
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Callable

    from economy_rows import EconomyCatalog, PendingReport
    from sqlalchemy import Connection, Engine
    from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.public_api_schema

STATEMENT_TIMEOUT_MILLISECONDS = 5_000
SHORT_STATEMENT_TIMEOUT_MILLISECONDS = 200
START = datetime(2026, 1, 1, tzinfo=UTC)
CTC_AMOUNT = "gov.irs.credits.ctc.amount.base[0].amount"
ELASTICITY = "gov.simulation.labor_supply_responses.elasticities.income"
WINDOW_YEARS = (2026, 2027)
# The .py version and so the catalog version of ``VERSIONS``, on a newer country
# package: a re-run on these versions joins no run on ``VERSIONS``.
NEWER_PACKAGE_VERSIONS = RunVersions(
    country_package_version="1.770.0", policyengine_version=POLICYENGINE_VERSION
)
REQUEST = EconomyRequest(
    country="us",
    policyengine_version=POLICYENGINE_VERSION,
    region_code="us",
    dataset_name=None,
    year=2026,
)
# What a store's session runs without naming a mapped class: the statement limit
# and the re-run lock, which read no table.
STATEMENTS_ON_NO_TABLE = ("set_config", "pg_advisory_xact_lock")

# Every function that runs a statement, whose first parameter is its session.
SESSION_FUNCTIONS = (
    (shared_rows, "find_or_insert_simulation"),
    (shared_rows, "find_or_insert_report"),
    (shared_rows, "find_or_insert_simulation_run"),
    (shared_rows, "find_or_insert_simulation_runs"),
    (shared_rows, "find_or_insert_window_run"),
    (shared_rows, "find_joinable_window_run"),
    (shared_rows, "find_or_insert_report_run"),
    (shared_rows, "find_joinable_report_run"),
    (shared_rows, "_find_or_insert"),
    (economy_run_records, "read_model_id"),
    (economy_run_records, "read_region"),
    (economy_run_records, "read_dataset"),
    (economy_run_records, "read_dataset_by_id"),
    (economy_run_records, "read_local_area_dataset"),
    (economy_run_records, "_dataset"),
    (economy_run_records, "insert_simulation_run"),
    (economy_run_records, "record_report_run_call"),
    (economy_run_records, "record_simulation_run_call"),
    (economy_run_records, "_record_call"),
    (economy_run_records, "fail_unstarted_report_run"),
    (economy_run_records, "fail_unstarted_simulation_run"),
    (economy_run_records, "_fail_unstarted"),
    (economy_run_reads, "read_economy_report"),
    (economy_run_reads, "read_economy_simulation"),
    (economy_rerun_records, "lock_reruns_of"),
    (economy_rerun_records, "insert_report_rerun"),
    (economy_rerun_records, "_insert_forced_report_run"),
    (economy_rerun_records, "insert_window_rerun"),
    (economy_rerun_records, "insert_household_report_rerun"),
    (economy_rerun_records, "insert_simulation_rerun"),
    (economy_inputs, "read_catalog_rows"),
    (economy_inputs, "read_simulation_values"),
    (economy_inputs, "read_side_values"),
    (economy_inputs, "read_report_sides"),
    (economy_inputs, "read_window_sides"),
    (keyed_economy_records, "find_or_create_economy_report"),
    (keyed_economy_records, "find_or_create_economy_simulation"),
    (keyed_economy_records, "_find_or_insert_simulations"),
    (keyed_economy_records, "find_or_insert_report_run_on_sides"),
    (keyed_economy_records, "find_or_insert_side_runs"),
    (keyed_window_records, "find_or_create_budget_window"),
    (keyed_window_records, "_find_or_insert_simulations"),
    (keyed_window_records, "insert_window_years"),
    (keyed_window_records, "stored_window_years"),
    (rerun_definitions, "read_report_definition"),
    (rerun_definitions, "_window_years"),
    (rerun_definitions, "read_simulation_definition"),
    (policy_reform, "read_policy_reform"),
    (policy_reform, "read_dynamic_reform"),
    (policy_reform, "_read_reform"),
)


def _keyed_report(
    economy: EconomyCatalog, *, reform: SimulationChoice = CURRENT_LAW
) -> KeyedEconomyReport:
    """Return a request's report on ``economy``, with fresh IDs to propose."""
    return KeyedEconomyReport(
        definition=EconomyReportDefinition(
            country=economy.catalog.country,
            baseline=CURRENT_LAW,
            reform=reform,
            dataset_name="populace_us_2024",
            region_code="us",
            year=2026,
            budget_window_size=None,
            spm=None,
        ),
        report_id=uuid4(),
        run_id=uuid4(),
        baseline=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
        reform=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
        tax_benefit_model_id=economy.catalog.model_id,
        population=economy.population(),
        versions=VERSIONS,
        inputs={},
    )


def _keyed_simulation(
    economy: EconomyCatalog, *, choice: SimulationChoice = CURRENT_LAW
) -> KeyedEconomySimulation:
    return KeyedEconomySimulation(
        definition=EconomySimulationDefinition(
            country=economy.catalog.country,
            choice=choice,
            dataset_name="populace_us_2024",
            region_code="us",
            year=2026,
            spm=None,
        ),
        simulation_id=uuid4(),
        run_id=uuid4(),
        population=economy.population(),
        versions=VERSIONS,
    )


def _keyed_window(
    economy: EconomyCatalog, *, reform: SimulationChoice = CURRENT_LAW
) -> KeyedBudgetWindow:
    return KeyedBudgetWindow(
        definition=EconomyReportDefinition(
            country=economy.catalog.country,
            baseline=CURRENT_LAW,
            reform=reform,
            dataset_name="populace_us_2024",
            region_code="us",
            year=WINDOW_YEARS[0],
            budget_window_size=len(WINDOW_YEARS),
            spm=None,
        ),
        report_id=uuid4(),
        window_run_id=uuid4(),
        tax_benefit_model_id=economy.catalog.model_id,
        population=economy.population(),
        versions=VERSIONS,
        inputs={},
        years=tuple(
            KeyedWindowYear(
                year=year,
                report_run_id=uuid4(),
                baseline=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
                reform=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
            )
            for year in WINDOW_YEARS
        ),
    )


@dataclass(frozen=True)
class _StoredRecords:
    """A catalog, with a stored report and a stored budget-window report on it."""

    economy: EconomyCatalog
    report_id: UUID
    window_report_id: UUID
    baseline: StoredRun
    reform: StoredRun


def _insert_stored_records(
    connection: Connection, economy: EconomyCatalog
) -> _StoredRecords:
    """Insert, as the superuser, the records the re-runs here calculate again."""
    baseline = insert_simulation_run(connection, economy.population())
    reform = insert_simulation_run(connection, economy.population())
    compared = (baseline, reform)
    report_id = insert_report_of(connection, economy, compared)
    insert_report_run_of(
        connection, report_id, compared, ReportRunOutcome(status=ReportRunStatus.FAILED)
    )
    window_report_id = insert_report_of(
        connection, economy, compared, budget_window_size=len(WINDOW_YEARS)
    )
    return _StoredRecords(
        economy=economy,
        report_id=report_id,
        window_report_id=window_report_id,
        baseline=baseline,
        reform=reform,
    )


def _rerun_side(stored: StoredRun) -> RerunSide:
    return RerunSide(simulation_id=stored.simulation_id, simulation_run_id=uuid4())


def _report_rerun(records: _StoredRecords, *, forced: bool = False) -> NewReportRerun:
    return NewReportRerun(
        report_id=records.report_id,
        run_id=uuid4(),
        population=records.economy.population(),
        versions=NEWER_PACKAGE_VERSIONS,
        baseline=_rerun_side(records.baseline),
        reform=_rerun_side(records.reform),
        forced=forced,
    )


def _window_rerun(records: _StoredRecords, *, forced: bool = False) -> NewWindowRerun:
    return NewWindowRerun(
        report_id=records.window_report_id,
        window_run_id=uuid4(),
        population=records.economy.population(),
        versions=NEWER_PACKAGE_VERSIONS,
        recalculate=forced,
        years=tuple(
            WindowRerunYear(
                year=year,
                report_run_id=uuid4(),
                baseline=_rerun_side(records.baseline),
                reform=_rerun_side(records.reform),
            )
            for year in WINDOW_YEARS
        ),
    )


def _simulation_rerun(records: _StoredRecords) -> NewSimulationRerun:
    return NewSimulationRerun(
        simulation_id=records.baseline.simulation_id,
        run_id=uuid4(),
        population=records.economy.population(),
        versions=NEWER_PACKAGE_VERSIONS,
    )


def _create_report(store: EconomyRunStore, records: _StoredRecords) -> object:
    return store.find_or_create_report(_keyed_report(records.economy))


def _create_simulation(store: EconomyRunStore, records: _StoredRecords) -> object:
    return store.find_or_create_simulation(_keyed_simulation(records.economy))


def _create_window(store: EconomyRunStore, records: _StoredRecords) -> object:
    return store.find_or_create_budget_window(_keyed_window(records.economy))


def _create_report_rerun(store: EconomyRunStore, records: _StoredRecords) -> object:
    return store.create_report_rerun(_report_rerun(records))


def _create_forced_report_rerun(
    store: EconomyRunStore, records: _StoredRecords
) -> object:
    return store.create_report_rerun(_report_rerun(records, forced=True))


def _create_window_rerun(store: EconomyRunStore, records: _StoredRecords) -> object:
    return store.create_window_rerun(_window_rerun(records))


def _create_forced_window_rerun(
    store: EconomyRunStore, records: _StoredRecords
) -> object:
    return store.create_window_rerun(_window_rerun(records, forced=True))


def _create_simulation_rerun(store: EconomyRunStore, records: _StoredRecords) -> object:
    return store.create_simulation_rerun(_simulation_rerun(records))


# Each way the store creates rows, and the first statement that writes the row
# the create exists to make.
CREATES: dict[str, tuple[Callable[[EconomyRunStore, _StoredRecords], object], str]] = {
    "report": (_create_report, "INSERT INTO public.report_runs"),
    "simulation": (_create_simulation, "INSERT INTO public.simulation_runs"),
    "budget window": (_create_window, "INSERT INTO public.budget_window_runs"),
    "report re-run": (_create_report_rerun, "INSERT INTO public.report_runs"),
    "forced report re-run": (
        _create_forced_report_rerun,
        "INSERT INTO public.report_runs",
    ),
    "window re-run": (_create_window_rerun, "INSERT INTO public.budget_window_runs"),
    "forced window re-run": (
        _create_forced_window_rerun,
        "INSERT INTO public.report_runs",
    ),
    "simulation re-run": (
        _create_simulation_rerun,
        "INSERT INTO public.simulation_runs",
    ),
}


@pytest.fixture
def session_factory(committed_engine) -> sessionmaker[Session]:
    return create_session_factory(committed_engine)


@pytest.fixture
def stored_records(committed_engine, committed_catalog) -> _StoredRecords:
    with committed_engine.begin() as connection:
        return _insert_stored_records(connection, committed_catalog)


def _store(
    session_factory: sessionmaker[Session],
    *,
    statement_timeout_milliseconds: int = STATEMENT_TIMEOUT_MILLISECONDS,
) -> EconomyRunStore:
    return EconomyRunStore.from_session_factory(
        session_factory,
        statement_timeout_milliseconds=statement_timeout_milliseconds,
    )


def _rows_under(engine: Engine, economy: EconomyCatalog) -> tuple[int, int, int]:
    """Count the catalog's simulation runs, report runs and window runs."""
    with engine.connect() as connection:
        counts = connection.execute(
            text(
                "SELECT (SELECT count(*) FROM public.simulation_runs "
                "WHERE tax_benefit_model_version_id = :version_id), "
                "(SELECT count(*) FROM public.report_runs WHERE report_id IN "
                "(SELECT id FROM public.reports WHERE tax_benefit_model_id = :model_id)"
                "), (SELECT count(*) FROM public.budget_window_runs WHERE report_id IN "
                "(SELECT id FROM public.reports WHERE tax_benefit_model_id = :model_id)"
                ")"
            ),
            {
                "version_id": economy.catalog.version_id,
                "model_id": economy.catalog.model_id,
            },
        ).one()
    return (counts[0], counts[1], counts[2])


def _only_statements_on_no_table(statements: list[str]) -> bool:
    return all(
        any(function in statement for function in STATEMENTS_ON_NO_TABLE)
        for statement in statements
    )


@pytest.mark.parametrize(
    ("module", "function_name"),
    SESSION_FUNCTIONS,
    ids=[f"{module.__name__}.{name}" for module, name in SESSION_FUNCTIONS],
)
def test_each_function_running_a_statement_takes_a_session(
    module: Any, function_name: str
) -> None:
    (first_parameter, *_) = inspect.signature(
        getattr(module, function_name)
    ).parameters.values()

    assert first_parameter.name == "session"
    assert first_parameter.annotation == "Session"


def test_the_store_is_built_on_a_session_factory_alone() -> None:
    assert "from_engine" not in vars(EconomyRunStore)
    assert "from_session_factory" in vars(EconomyRunStore)


def test_each_lookup_names_the_mapped_class_it_inserts_into() -> None:
    assert shared_rows._SIMULATION_DEFINITION.model is Simulation
    assert shared_rows._REPORT_DEFINITION.model is Report
    assert shared_rows._JOINABLE_SIMULATION_RUN.model is SimulationRun
    assert shared_rows._JOINABLE_REPORT_RUN.model is ReportRun
    assert shared_rows._JOINABLE_WINDOW_RUN.model is BudgetWindowRun


def test_each_refused_key_is_known_by_its_table_and_column() -> None:
    """Every foreign key of the five classes is found through their arguments."""
    expected = {
        str(constraint.name): (model.__tablename__, constraint.column_keys[0])
        for model in (Simulation, SimulationRun, Report, ReportRun, BudgetWindowRun)
        for constraint in model.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }

    assert expected
    assert expected == economy_run_records._KEY_COLUMNS


def test_every_economy_read_names_mapped_classes(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(
            connection, parameter_names=(CTC_AMOUNT, ELASTICITY)
        )
        policy_id = insert_policy(
            connection, economy.catalog, [StoredValue(CTC_AMOUNT, 3_000, START)]
        )
        dynamic_id = insert_dynamic(
            connection, economy.catalog, [StoredValue(ELASTICITY, -0.05, START)]
        )
        records = _insert_stored_records(connection, economy)
        act_as_the_service(connection)

        with session_in(connection) as session:
            core_statements = statements_naming_no_mapped_class(session)
            catalog = economy_inputs.read_catalog_rows(session, REQUEST)
            sides = economy_inputs.read_report_sides(
                session,
                REQUEST,
                catalog,
                baseline=CURRENT_LAW,
                reform=SimulationChoice(policy_id=policy_id, dynamic_id=dynamic_id),
            )
            local_area_dataset = economy_run_records.read_local_area_dataset(
                session, catalog_version_id=economy.catalog.version_id
            )
            named_dataset = economy_run_records.read_dataset(
                session,
                catalog_version_id=economy.catalog.version_id,
                name="populace_us_2024",
            )
            report_state = read_economy_report(session, records.report_id)
            simulation_state = read_economy_simulation(
                session, records.baseline.simulation_id
            )
            report_definition = read_report_definition(session, records.report_id)
            window_definition = read_report_definition(
                session, records.window_report_id
            )
            simulation_definition = read_simulation_definition(
                session, records.baseline.simulation_id
            )

    assert core_statements == []
    assert catalog.dataset.name == named_dataset.name == "populace_us_2024"
    assert sides.reform.reform == {
        CTC_AMOUNT: {"2026-01-01.2200-12-31": 3_000},
        ELASTICITY: {"2026-01-01.2200-12-31": -0.05},
    }
    assert local_area_dataset is None
    assert report_state.run.status is ReportRunStatus.FAILED
    assert simulation_state.run.status is SimulationStatus.SUCCEEDED
    assert report_definition.kind == "economy"
    assert report_definition.region_code == "us"
    assert window_definition.kind == "budget_window"
    assert simulation_definition.country == "us"


def test_every_economy_write_names_mapped_classes(public_api_database_url) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        policy_id = insert_policy(connection, economy.catalog)
        records = _insert_stored_records(connection, economy)
        pending = insert_pending_report(connection, economy)
        act_as_the_service(connection)
        reform = SimulationChoice(policy_id=policy_id, dynamic_id=None)

        with session_in(connection) as session:
            core_statements = statements_naming_no_mapped_class(session)
            report = find_or_create_economy_report(
                session, _keyed_report(economy, reform=reform)
            )
            simulation = find_or_create_economy_simulation(
                session, _keyed_simulation(economy, choice=reform)
            )
            window = find_or_create_budget_window(
                session, _keyed_window(economy, reform=reform)
            )
            insert_report_rerun(session, _report_rerun(records))
            insert_report_rerun(session, _report_rerun(records, forced=True))
            insert_window_rerun(session, _window_rerun(records))
            insert_window_rerun(session, _window_rerun(records, forced=True))
            insert_simulation_rerun(session, _simulation_rerun(records))
            record_report_run_call(
                session, report_run_id=report.run.id, modal_call_id="fc-report"
            )
            record_simulation_run_call(
                session, simulation_run_id=simulation.run.id, modal_call_id="fc-sim"
            )
            failed_report = fail_unstarted_report_run(
                session,
                report_run_id=pending.report_run_id,
                simulation_run_ids=pending.simulation_run_ids,
                error_message="Modal refused the call.",
            )
            failed_simulation = fail_unstarted_simulation_run(
                session,
                simulation_run_id=pending.baseline.simulation_run_id,
                error_message="already failed with its report",
            )

    assert _only_statements_on_no_table(core_statements)
    assert report.run.inserted is True
    assert window.window_run.inserted is True
    assert failed_report is True
    assert failed_simulation is False


def test_a_find_or_insert_sends_one_conflict_handling_insert_per_lookup(
    public_api_database_url,
) -> None:
    """Each insert names its unique index, partial or whole, and does nothing."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)
        statements = statements_sent(connection)

        in_session(connection, find_or_create_economy_report, _keyed_report(economy))
        in_session(connection, find_or_create_budget_window, _keyed_window(economy))

    inserts = [
        " ".join(statement.split())
        for statement in statements
        if statement.startswith("INSERT INTO")
    ]
    assert any(
        statement.startswith("INSERT INTO public.simulations ")
        and "ON CONFLICT (key_version, definition_key) DO NOTHING" in statement
        for statement in inserts
    )
    assert any(
        statement.startswith("INSERT INTO public.reports ")
        and "ON CONFLICT (key_version, definition_key) DO NOTHING" in statement
        for statement in inserts
    )
    assert any(
        statement.startswith("INSERT INTO public.simulation_runs ")
        and "WHERE status <> 'failed' AND NOT forced DO NOTHING" in statement
        for statement in inserts
    )
    assert any(
        statement.startswith("INSERT INTO public.report_runs ")
        and "budget_window_run_id IS NULL AND status <> 'failed' AND NOT forced "
        "DO NOTHING"
        in statement
        for statement in inserts
    )
    assert any(
        statement.startswith("INSERT INTO public.budget_window_runs ")
        and "ON CONFLICT" in statement
        and "NOT recalculate DO NOTHING" in statement
        for statement in inserts
    )


def test_a_report_goes_through_the_stores_sessions_and_stays_readable(
    committed_engine, committed_catalog, session_factory
) -> None:
    core_statements = statements_naming_no_mapped_class(session_factory)
    store = _store(session_factory)

    found = store.find_or_create_report(_keyed_report(committed_catalog))
    joined = store.find_or_create_report(_keyed_report(committed_catalog))
    status = store.record_report_call(found.run.id, "fc-report")
    state = store.read_report(found.report_id)
    definition = store.read_report_definition(found.report_id)

    assert _only_statements_on_no_table(core_statements)
    assert (found.run.inserted, joined.run.inserted) == (True, False)
    assert joined.run.id == found.run.id
    assert status is ReportRunStatus.PENDING
    assert state.run.id == found.run.id
    assert state.run.created_at is not None
    assert definition.keyed is True
    with committed_engine.connect() as connection:
        assert (
            connection.execute(
                select(ReportRun.modal_call_id).where(ReportRun.id == found.run.id)
            ).scalar_one()
            == "fc-report"
        )


def test_a_simulation_goes_through_the_stores_sessions_and_stays_readable(
    committed_catalog, session_factory
) -> None:
    core_statements = statements_naming_no_mapped_class(session_factory)
    store = _store(session_factory)

    found = store.find_or_create_simulation(_keyed_simulation(committed_catalog))
    failed = store.fail_unstarted_simulation(found.run.id, "Modal refused the call.")
    state = store.read_simulation(found.simulation_id)
    definition = store.read_simulation_definition(found.simulation_id)

    assert _only_statements_on_no_table(core_statements)
    assert failed is True
    assert state.run.status is SimulationStatus.FAILED
    assert state.run.error_message == "Modal refused the call."
    assert state.run.completed_at is not None
    assert definition.kind == "economy"


def test_a_budget_window_and_its_reruns_go_through_the_stores_sessions(
    committed_catalog, stored_records, session_factory
) -> None:
    core_statements = statements_naming_no_mapped_class(session_factory)
    store = _store(session_factory)
    rerun = _window_rerun(stored_records)

    created = store.find_or_create_budget_window(_keyed_window(committed_catalog))
    joined = store.find_or_create_budget_window(_keyed_window(committed_catalog))
    rerun_created = store.create_window_rerun(rerun)
    rerun_joined = store.create_window_rerun(replace(rerun, window_run_id=uuid4()))

    assert _only_statements_on_no_table(core_statements)
    assert (created.window_run.inserted, joined.window_run.inserted) == (True, False)
    assert [year.year for year in joined.years] == list(WINDOW_YEARS)
    assert rerun_created.window_run.inserted is True
    assert rerun_joined.window_run.id == rerun_created.window_run.id
    assert rerun_joined.window_run.inserted is False
    assert [year.report_run_id for year in rerun_joined.years] == [
        year.report_run_id for year in rerun_created.years
    ]


@pytest.mark.parametrize("create_name", CREATES)
def test_a_connection_lost_at_a_creates_commit_leaves_the_outcome_unknown(
    committed_engine, stored_records, session_factory, create_name: str
) -> None:
    """Every statement ran, and PostgreSQL may have applied the commit."""
    create, _ = CREATES[create_name]
    unflushed_counts = end_connection_at(
        session_factory, "before_commit", committed_engine
    )

    with pytest.raises(EconomyRecordOutcomeUnknownError) as raised:
        create(_store(session_factory), stored_records)

    assert isinstance(raised.value.__cause__, OperationalError)
    # Nothing was left for the commit's flush to send.
    assert unflushed_counts == [0]


@pytest.mark.parametrize("create_name", CREATES)
def test_a_connection_lost_during_a_creates_statements_is_not_a_lost_commit(
    committed_engine, stored_records, session_factory, create_name: str
) -> None:
    create, statement_start = CREATES[create_name]
    before = _rows_under(committed_engine, stored_records.economy)
    ended_backends = end_connection_before_statement(committed_engine, statement_start)

    with pytest.raises(OperationalError):
        create(_store(session_factory), stored_records)

    assert len(ended_backends) == 1
    assert _rows_under(committed_engine, stored_records.economy) == before


def test_a_statement_outlasting_the_limit_is_not_a_lost_commit(
    committed_engine, stored_records, session_factory
) -> None:
    """The limit covers each statement, and a cancelled INSERT wrote nothing."""
    store = _store(
        session_factory,
        statement_timeout_milliseconds=SHORT_STATEMENT_TIMEOUT_MILLISECONDS,
    )
    before = _rows_under(committed_engine, stored_records.economy)

    with committed_engine.begin() as locking_connection:
        locking_connection.execute(
            text("LOCK TABLE public.simulation_runs IN ACCESS EXCLUSIVE MODE")
        )
        with pytest.raises(OperationalError, match="statement timeout"):
            store.create_simulation_rerun(_simulation_rerun(stored_records))

    assert _rows_under(committed_engine, stored_records.economy) == before


def test_a_refused_reference_is_named_and_is_not_a_lost_commit(
    committed_engine, stored_records, session_factory
) -> None:
    economy = stored_records.economy
    before = _rows_under(committed_engine, economy)
    missing_policy = SimulationChoice(policy_id=uuid4(), dynamic_id=None)

    with pytest.raises(EconomyRecordReferenceError) as refused:
        _store(session_factory).find_or_create_simulation(
            _keyed_simulation(economy, choice=missing_policy)
        )

    assert (refused.value.table_name, refused.value.column_name) == (
        "simulations",
        "policy_id",
    )
    assert _rows_under(committed_engine, economy) == before


@pytest.mark.parametrize(
    "create_with_a_deleted_record",
    [
        lambda store, records: store.create_report_rerun(
            replace(_report_rerun(records), report_id=uuid4())
        ),
        lambda store, records: store.create_report_rerun(
            replace(_report_rerun(records, forced=True), report_id=uuid4())
        ),
        lambda store, records: store.create_window_rerun(
            replace(_window_rerun(records), report_id=uuid4())
        ),
        lambda store, records: store.create_window_rerun(
            replace(_window_rerun(records, forced=True), report_id=uuid4())
        ),
        lambda store, records: store.create_simulation_rerun(
            replace(_simulation_rerun(records), simulation_id=uuid4())
        ),
    ],
    ids=[
        "report",
        "forced report",
        "window",
        "forced window",
        "simulation",
    ],
)
def test_a_rerun_of_a_deleted_record_says_the_record_is_gone(
    committed_engine, stored_records, session_factory, create_with_a_deleted_record
) -> None:
    before = _rows_under(committed_engine, stored_records.economy)

    with pytest.raises(RerunRecordDeletedError):
        create_with_a_deleted_record(_store(session_factory), stored_records)

    assert _rows_under(committed_engine, stored_records.economy) == before


def test_an_unforced_rerun_refused_inside_its_savepoint_keeps_nothing(
    committed_engine, stored_records, session_factory
) -> None:
    """The reform's simulation is gone: the baseline's new run is taken back."""
    rerun = replace(
        _report_rerun(stored_records),
        reform=RerunSide(
            simulation_id=UUID("ffffffff-ffff-4fff-bfff-ffffffffffff"),
            simulation_run_id=uuid4(),
        ),
    )
    before = _rows_under(committed_engine, stored_records.economy)
    statements = statements_sent(committed_engine)

    with pytest.raises(EconomyRecordReferenceError) as refused:
        _store(session_factory).create_report_rerun(rerun)

    assert (refused.value.table_name, refused.value.column_name) == (
        "simulation_runs",
        "simulation_id",
    )
    assert [
        statement.split(" sa_savepoint")[0]
        for statement in statements
        if "SAVEPOINT" in statement
    ] == ["SAVEPOINT", "ROLLBACK TO SAVEPOINT"]
    assert _rows_under(committed_engine, stored_records.economy) == before


def test_a_report_losing_its_run_on_a_session_joins_it_and_keeps_nothing(
    committed_engine, committed_catalog, session_factory
) -> None:
    """Another request inserts the report run between the lookup and the insert."""
    economy = committed_catalog
    store = _store(session_factory)
    first = store.find_or_create_report(_keyed_report(economy))
    with committed_engine.begin() as connection:
        for run_table, run_id in (
            ("simulation_runs", first.baseline.simulation_run_id),
            ("report_runs", first.run.id),
        ):
            connection.execute(
                text(
                    f"UPDATE public.{run_table} SET status = 'failed', "  # noqa: S608  # fixed table names
                    "completed_at = now() WHERE id = :id"
                ),
                {"id": run_id},
            )
    competing: list[UUID] = []
    request = _keyed_report(economy)

    def create_a_competing_run_first(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if competing or not statement.startswith("INSERT INTO public.report_runs"):
            return
        run_id = uuid4()
        competing.append(run_id)
        with committed_engine.begin() as other:
            other.execute(
                text(
                    "INSERT INTO public.report_runs (id, report_id, "
                    "baseline_simulation_run_id, reform_simulation_run_id, "
                    "country_package_version, policyengine_version, status) "
                    "SELECT :id, report_id, :side_run, :side_run, "
                    "country_package_version, policyengine_version, 'pending' "
                    "FROM public.report_runs WHERE id = :failed_run"
                ),
                {
                    "id": run_id,
                    "side_run": first.reform.simulation_run_id,
                    "failed_run": first.run.id,
                },
            )

    event.listen(
        committed_engine, "before_cursor_execute", create_a_competing_run_first
    )

    joined = store.find_or_create_report(request)

    assert (joined.run.id, joined.run.inserted) == (competing[0], False)
    assert joined.baseline.run_inserted is False
    with committed_engine.connect() as connection:
        kept = connection.execute(
            select(func.count())
            .select_from(SimulationRun)
            .where(SimulationRun.id == request.baseline.simulation_run_id)
        ).scalar_one()
    assert kept == 0


def _at_once(
    engine: Engine,
    session_factory: sessionmaker[Session],
    work: Callable[[Session, str], None],
) -> list[str]:
    """Run ``work`` in two sessions, the first holding until the second waits.

    Return the statement the second was waiting in.
    """
    first_has_written = threading.Event()
    waited_on: list[str] = []

    def first_request() -> None:
        with session_factory.begin() as session:
            act_as_the_service(session)
            work(session, "first")
            holder_pid = session.execute(select(func.pg_backend_pid())).scalar_one()
            first_has_written.set()
            waited_on.append(wait_until_another_backend_waits_for(engine, holder_pid))

    def second_request() -> None:
        assert first_has_written.wait(LOCK_WAIT_TIMEOUT_SECONDS)
        with session_factory.begin() as session:
            act_as_the_service(session)
            work(session, "second")

    run_together((first_request, second_request), released=first_has_written)
    return waited_on


def test_two_identical_report_requests_at_once_end_on_one_run(
    committed_engine, committed_catalog, session_factory
) -> None:
    found: dict[str, Any] = {}

    def create(session: Session, name: str) -> None:
        found[name] = find_or_create_economy_report(
            session, _keyed_report(committed_catalog)
        )

    _at_once(committed_engine, session_factory, create)

    first, second = found["first"], found["second"]
    assert (second.report_id, second.run.id) == (first.report_id, first.run.id)
    assert (first.run.inserted, second.run.inserted) == (True, False)
    assert second.baseline == replace(first.baseline, run_inserted=False)


def test_two_identical_simulation_requests_at_once_end_on_one_run(
    committed_engine, committed_catalog, session_factory
) -> None:
    found: dict[str, Any] = {}

    def create(session: Session, name: str) -> None:
        found[name] = find_or_create_economy_simulation(
            session, _keyed_simulation(committed_catalog)
        )

    _at_once(committed_engine, session_factory, create)

    first, second = found["first"], found["second"]
    assert second.simulation_id == first.simulation_id
    assert second.run.id == first.run.id
    assert (first.run.inserted, second.run.inserted) == (True, False)


def test_two_identical_window_requests_at_once_end_on_one_window_run(
    committed_engine, committed_catalog, session_factory
) -> None:
    found: dict[str, Any] = {}

    def create(session: Session, name: str) -> None:
        found[name] = find_or_create_budget_window(
            session, _keyed_window(committed_catalog)
        )

    _at_once(committed_engine, session_factory, create)

    first, second = found["first"], found["second"]
    assert second.window_run.id == first.window_run.id
    assert (first.window_run.inserted, second.window_run.inserted) == (True, False)
    assert [year.report_run_id for year in second.years] == [
        year.report_run_id for year in first.years
    ]


def test_a_second_unforced_rerun_waits_for_the_first_and_joins_its_run(
    committed_engine, stored_records, session_factory
) -> None:
    """The re-run lock holds it until the first transaction has committed."""
    found: dict[str, Any] = {}

    def rerun(session: Session, name: str) -> None:
        found[name] = insert_report_rerun(session, _report_rerun(stored_records))

    waited_on = _at_once(committed_engine, session_factory, rerun)

    assert "pg_advisory_xact_lock" in waited_on[0]
    assert (found["first"].inserted, found["second"].inserted) == (True, False)
    assert found["second"].id == found["first"].id


def test_a_second_unforced_window_rerun_waits_for_the_first_and_joins_it(
    committed_engine, stored_records, session_factory
) -> None:
    found: dict[str, Any] = {}

    def rerun(session: Session, name: str) -> None:
        found[name] = insert_window_rerun(session, _window_rerun(stored_records))

    waited_on = _at_once(committed_engine, session_factory, rerun)

    assert "pg_advisory_xact_lock" in waited_on[0]
    first, second = found["first"], found["second"]
    assert second.window_run.id == first.window_run.id
    assert (first.window_run.inserted, second.window_run.inserted) == (True, False)


def _committed_pending_report(engine: Engine, economy: EconomyCatalog) -> PendingReport:
    with engine.begin() as connection:
        return insert_pending_report(connection, economy)


def test_a_call_recorded_at_once_by_two_sessions_keeps_the_first(
    committed_engine, committed_catalog, session_factory
) -> None:
    """The second waits on the row, then finds it no longer without a call."""
    pending = _committed_pending_report(committed_engine, committed_catalog)
    statuses: dict[str, ReportRunStatus | None] = {}

    def record(session: Session, name: str) -> None:
        statuses[name] = record_report_run_call(
            session, report_run_id=pending.report_run_id, modal_call_id=f"fc-{name}"
        )

    waited_on = _at_once(committed_engine, session_factory, record)

    assert waited_on[0].lstrip().startswith("UPDATE public.report_runs")
    assert statuses == {
        "first": ReportRunStatus.PENDING,
        "second": ReportRunStatus.PENDING,
    }
    with committed_engine.connect() as connection:
        recorded = connection.execute(
            select(ReportRun.modal_call_id).where(ReportRun.id == pending.report_run_id)
        ).scalar_one()
    assert recorded == "fc-first"


def test_a_failure_after_a_call_was_recorded_at_once_changes_nothing(
    committed_engine, committed_catalog, session_factory
) -> None:
    """A run whose call another session records first is left to that call."""
    pending = _committed_pending_report(committed_engine, committed_catalog)
    failed: list[bool] = []

    def record_or_fail(session: Session, name: str) -> None:
        if name == "first":
            record_report_run_call(
                session, report_run_id=pending.report_run_id, modal_call_id="fc-first"
            )
            return
        failed.append(
            fail_unstarted_report_run(
                session,
                report_run_id=pending.report_run_id,
                simulation_run_ids=pending.simulation_run_ids,
                error_message="Modal refused the call.",
            )
        )

    _at_once(committed_engine, session_factory, record_or_fail)

    assert failed == [False]
    with committed_engine.connect() as connection:
        statuses = connection.execute(
            select(ReportRun.status, ReportRun.error_message).where(
                ReportRun.id == pending.report_run_id
            )
        ).one()
    assert statuses == (ReportRunStatus.PENDING, None)


def test_each_conditional_update_is_one_statement_that_leaves_loaded_rows_alone(
    public_api_database_url,
) -> None:
    """A read after it returns the update, whatever the session has loaded."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        pending = insert_pending_report(connection, economy)
        act_as_the_service(connection)
        statements = statements_sent(connection)

        with session_in(connection) as session:
            loaded_run = session.get(ReportRun, pending.report_run_id)
            failed = fail_unstarted_report_run(
                session,
                report_run_id=pending.report_run_id,
                simulation_run_ids=(),
                error_message="Modal refused the call.",
            )
            state = read_economy_report(session, pending.report_id)
            loaded_status = loaded_run.status

    updates = [
        " ".join(statement.split())
        for statement in statements
        if statement.startswith("UPDATE public.report_runs")
    ]
    assert failed is True
    assert len(updates) == 1
    assert "status =" in updates[0]
    assert "modal_call_id IS NULL" in updates[0]
    assert "RETURNING" in updates[0]
    assert state.run.status is ReportRunStatus.FAILED
    assert state.run.completed_at is not None
    # The loaded object is the session's own; the update didn't touch it.
    assert loaded_status is ReportRunStatus.PENDING


def test_the_statements_of_a_forced_window_rerun_mark_every_new_run_forced(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        records = _insert_stored_records(connection, economy)
        act_as_the_service(connection)
        rerun = _window_rerun(records, forced=True)

        with session_in(connection) as session:
            found = insert_window_rerun(session, rerun)
            year_runs = session.execute(
                select(
                    ReportRun.year,
                    ReportRun.forced,
                    ReportRun.modal_call_id,
                    ReportRun.status,
                )
                .where(ReportRun.budget_window_run_id == rerun.window_run_id)
                .order_by(ReportRun.year)
            ).all()
            forced_side_runs = session.execute(
                select(func.count())
                .select_from(SimulationRun)
                .where(
                    SimulationRun.id.in_(
                        [year.baseline.simulation_run_id for year in rerun.years]
                        + [year.reform.simulation_run_id for year in rerun.years]
                    ),
                    SimulationRun.forced.is_(True),
                )
            ).scalar_one()

    assert found.window_run.inserted is True
    assert [tuple(row) for row in year_runs] == [
        (year, True, None, ReportRunStatus.PENDING) for year in WINDOW_YEARS
    ]
    assert forced_side_runs == 2 * len(WINDOW_YEARS)


def test_a_pending_simulation_run_the_session_inserts_is_a_stored_row(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        stored = insert_simulation_run(
            connection, economy.population(), RunShape(status=SimulationStatus.FAILED)
        )
        act_as_the_service(connection)
        run_id = uuid4()

        with session_in(connection) as session:
            economy_run_records.insert_simulation_run(
                session,
                run_id=run_id,
                simulation_id=stored.simulation_id,
                population=economy.population(),
                versions=VERSIONS,
                forced=True,
            )
            stored_run = session.execute(
                select(
                    SimulationRun.status,
                    SimulationRun.forced,
                    SimulationRun.modal_call_id,
                ).where(SimulationRun.id == run_id)
            ).one()

    assert tuple(stored_run) == (SimulationStatus.PENDING, True, None)


def test_a_windows_year_runs_are_inserted_with_every_column_they_set(
    public_api_database_url,
) -> None:
    """A year's run sets ``modal_call_id`` to NULL, as a single insert would."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        records = _insert_stored_records(connection, economy)
        act_as_the_service(connection)
        statements = statements_sent(connection)

        in_session(connection, insert_window_rerun, _window_rerun(records, forced=True))

    (year_runs_insert,) = {
        statement
        for statement in statements
        if statement.startswith("INSERT INTO public.report_runs")
    }
    assert "modal_call_id" in year_runs_insert


def test_a_key_refused_among_a_windows_year_runs_is_named(
    public_api_database_url,
) -> None:
    """The multi-row insert's refusal names the key, as a single insert's does."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        records = _insert_stored_records(connection, economy)
        act_as_the_service(connection)
        rerun = _window_rerun(records, forced=True)
        years = tuple(
            WindowYearToInsert(
                year=year.year,
                report_run_id=year.report_run_id,
                simulation_ids={
                    "baseline": year.baseline.simulation_id,
                    "reform": year.reform.simulation_id,
                },
                proposed_run_ids={
                    "baseline": year.baseline.simulation_run_id,
                    "reform": year.reform.simulation_run_id,
                },
            )
            for year in rerun.years
        )

        with session_in(connection) as session:
            window_run = find_or_insert_window_run(
                session,
                window_run_row(
                    run_id=rerun.window_run_id,
                    report_id=records.window_report_id,
                    population=rerun.population,
                    versions=rerun.versions,
                    recalculate=False,
                ),
            )
            savepoint = session.begin_nested()
            with (
                pytest.raises(EconomyRecordReferenceError) as refused,
                references_checked(),
            ):
                insert_window_years(
                    session,
                    years,
                    report_id=uuid4(),
                    window_run_id=window_run.id,
                    population=rerun.population,
                    versions=rerun.versions,
                    forced=True,
                )
            savepoint.rollback()

    assert (refused.value.table_name, refused.value.column_name) == (
        "report_runs",
        "report_id",
    )


class _SessionFindingNothing:
    """A session whose inserts conflict and whose reads then find no row."""

    def __init__(self) -> None:
        self.statements: list[object] = []

    def execute(self, statement: object) -> _SessionFindingNothing:
        self.statements.append(statement)
        return self

    def one_or_none(self) -> None:
        return None


def test_a_lookup_gives_up_on_a_run_that_keeps_failing_before_its_read() -> None:
    session = _SessionFindingNothing()
    run = {
        "simulation_id": uuid4(),
        "year": 2026,
        "tax_benefit_model_version_id": uuid4(),
        "country_package_version": VERSIONS.country_package_version,
        "policyengine_version": VERSIONS.policyengine_version,
    }

    with pytest.raises(SharedRowNotSettledError, match="simulation_runs"):
        shared_rows.find_or_insert_simulation_run(session, run)

    assert len(session.statements) == 2 * shared_rows.INSERT_ATTEMPTS
