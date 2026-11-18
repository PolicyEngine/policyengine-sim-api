"""Household jobs and reports on ORM sessions, against a real database.

The household code runs its statements through ORM sessions on the mapped
classes. A flush is where an added row's INSERT runs, so what a failure means
depends on when it happens: during the flush nothing was written, and at the
commit after a flush the rows may exist. These tests make each failure happen
for real, by ending the session's connection at that moment.

They use a database the Public API's own v2 migration chain built
(``PUBLIC_API_SCHEMA_DATABASE_URL``). A test of the stores' transactions needs
rows it can commit, so it commits a catalog of its own and deletes it after;
the others work in one transaction rolled back afterwards.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from committed_rows import (
    LOCK_WAIT_TIMEOUT_SECONDS,
    delete_catalog_rows,
    run_together,
    wait_until_another_backend_waits_for,
)
from conftest import (
    act_as_the_service,
    in_session,
    rolled_back_transaction,
    session_in,
)
from economy_rows import POLICYENGINE_VERSION, VERSIONS, insert_economy_catalog
from household_report_rows import insert_household_report_rows
from policyengine_simulation_persistence import (
    EconomyRecordOutcomeUnknownError,
    EconomyRecordReferenceError,
    EconomyRequest,
    HouseholdJob,
    HouseholdJobStatus,
    HouseholdJobStore,
    HouseholdReportDefinition,
    HouseholdReportStore,
    HouseholdRerunSide,
    KeyedHouseholdReport,
    NewHouseholdJob,
    NewHouseholdReportRerun,
    ProposedSide,
    ReportRunStatus,
    SimulationChoice,
    SimulationRun,
    create_session_factory,
    fail_unstarted_household_job,
    find_or_create_household_report,
    insert_household_job,
    read_catalog_version_id,
    read_household_job,
    read_stored_household,
    record_household_call,
)
from policyengine_simulation_persistence.economy_inputs import read_catalog_rows
from policyengine_simulation_persistence.household_report_records import (
    household_simulation_run_row,
    insert_household_simulation_run,
)
from policyengine_simulation_persistence.keyed_economy_records import (
    find_or_insert_report_run_on_sides,
)
from public_api_rows import insert_household
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from economy_rows import EconomyCatalog
    from public_api_rows import CatalogVersion
    from sqlalchemy import Connection, Engine
    from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.public_api_schema

STATEMENT_TIMEOUT_MILLISECONDS = 5_000
SHORT_STATEMENT_TIMEOUT_MILLISECONDS = 200
# How long PostgreSQL may take to end a backend it is told to end.
TERMINATE_TIMEOUT_MILLISECONDS = 5_000
CURRENT_LAW = SimulationChoice(policy_id=None, dynamic_id=None)
DOCUMENT = {
    "people": [
        {
            "id": "you",
            "values": {"age": 40},
            "memberships": {"household": "your household"},
        }
    ],
    "household": [{"id": "your household", "values": {}}],
}


@dataclass(frozen=True)
class _CommittedHousehold:
    """A committed catalog version and a committed household."""

    economy: EconomyCatalog
    household_id: UUID

    @property
    def catalog(self) -> CatalogVersion:
        return self.economy.catalog


@pytest.fixture
def committed_household(
    committed_engine, committed_catalog
) -> Iterator[_CommittedHousehold]:
    """Commit a household; delete it after, with every row naming it."""
    with committed_engine.begin() as connection:
        household_id = insert_household(
            connection, country="us", default_year=2026, document=DOCUMENT
        )
    try:
        yield _CommittedHousehold(committed_catalog, household_id)
    finally:
        with committed_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM public.household_jobs WHERE household_id = :id"),
                {"id": household_id},
            )
            delete_catalog_rows(connection, committed_catalog)
            connection.execute(
                text("DELETE FROM public.households WHERE id = :id"),
                {"id": household_id},
            )


@pytest.fixture
def session_factory(committed_engine) -> sessionmaker[Session]:
    return create_session_factory(committed_engine)


def _new_job(catalog: CatalogVersion, household_id: UUID) -> NewHouseholdJob:
    return NewHouseholdJob(
        id=uuid4(),
        country="us",
        household_id=household_id,
        policy_id=None,
        year=2026,
        request_data={"country": "us"},
        tax_benefit_model_version_id=catalog.version_id,
        country_package_version=VERSIONS.country_package_version,
        policyengine_version=POLICYENGINE_VERSION,
    )


def _job_store(
    session_factory: sessionmaker[Session],
    *,
    statement_timeout_milliseconds: int = STATEMENT_TIMEOUT_MILLISECONDS,
) -> HouseholdJobStore:
    return HouseholdJobStore.from_session_factory(
        session_factory,
        statement_timeout_milliseconds=statement_timeout_milliseconds,
    )


def _report_store(session_factory: sessionmaker[Session]) -> HouseholdReportStore:
    return HouseholdReportStore.from_session_factory(
        session_factory,
        statement_timeout_milliseconds=STATEMENT_TIMEOUT_MILLISECONDS,
    )


def _end_connection_at(
    session_factory: sessionmaker[Session], session_event: str, engine: Engine
) -> list[int]:
    """End each session's connection at ``session_event``, from another one.

    Return a list that gains, each time, how many added rows the session had
    not yet flushed.
    """
    unflushed_counts: list[int] = []

    def end_the_connection(session: Session, *_event_details: object) -> None:
        unflushed_counts.append(len(session.new))
        backend_id = session.execute(select(func.pg_backend_pid())).scalar_one()
        with engine.connect() as connection:
            ended = connection.execute(
                select(
                    func.pg_terminate_backend(
                        backend_id, TERMINATE_TIMEOUT_MILLISECONDS
                    )
                )
            ).scalar_one()
        assert ended

    event.listen(session_factory, session_event, end_the_connection)
    return unflushed_counts


def _count(engine: Engine, table_name: str, row_id: UUID) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text(f"SELECT count(*) FROM public.{table_name} WHERE id = :id"),  # noqa: S608  # fixed table names
            {"id": row_id},
        ).scalar_one()


def _orm_statement_flags(session: Session) -> list[bool]:
    """Return a list that gains, per statement, whether it names mapped classes."""
    flags: list[bool] = []
    event.listen(
        session,
        "do_orm_execute",
        lambda execute_state: flags.append(execute_state.is_orm_statement),
    )
    return flags


def _flushed_rows(session: Session) -> list[object]:
    """Return a list that gains each new mapped object the session flushes."""
    flushed: list[object] = []
    event.listen(
        session,
        "after_flush",
        lambda flushing_session, _flush_context: flushed.extend(flushing_session.new),
    )
    return flushed


def _stored_job(connection: Connection) -> NewHouseholdJob:
    """Insert, as the superuser, the rows a job names; return a job naming them."""
    economy = insert_economy_catalog(connection)
    household_id = insert_household(
        connection, country="us", default_year=2026, document=DOCUMENT
    )
    return _new_job(economy.catalog, household_id)


def _keyed_report(catalog: CatalogVersion, household_id: UUID) -> KeyedHouseholdReport:
    """Return a request's report of the baseline alone, with fresh IDs to propose."""
    return KeyedHouseholdReport(
        definition=HouseholdReportDefinition(
            country="us",
            baseline=CURRENT_LAW,
            reform=None,
            household_id=household_id,
            year=2026,
        ),
        report_id=uuid4(),
        run_id=uuid4(),
        baseline=ProposedSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
        reform=None,
        tax_benefit_model_id=catalog.model_id,
        tax_benefit_model_version_id=catalog.version_id,
        versions=VERSIONS,
    )


def _forced_rerun(
    committed_engine: Engine, household: _CommittedHousehold
) -> NewHouseholdReportRerun:
    """Commit a stored report of the baseline alone; return its forced re-run."""
    with committed_engine.begin() as connection:
        report = insert_household_report_rows(
            connection,
            household.catalog,
            household_id=household.household_id,
            year=2026,
            versions=VERSIONS,
            with_reform=False,
        )
    return NewHouseholdReportRerun(
        report_id=report.id,
        run_id=uuid4(),
        tax_benefit_model_version_id=household.catalog.version_id,
        year=report.year,
        versions=VERSIONS,
        baseline=HouseholdRerunSide(
            simulation_id=report.baseline.simulation_id, simulation_run_id=uuid4()
        ),
        reform=None,
        forced=True,
    )


def test_the_job_functions_run_orm_statements_on_their_session(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        new_job = _stored_job(connection)
        act_as_the_service(connection)

        with session_in(connection) as session:
            orm_statements = _orm_statement_flags(session)
            read_catalog_version_id(
                session, country="us", policyengine_version=POLICYENGINE_VERSION
            )
            read_stored_household(
                session, country="us", household_id=new_job.household_id
            )
            insert_household_job(session, new_job)
            record_household_call(session, job_id=new_job.id, modal_call_id="fc-1")
            fail_unstarted_household_job(
                session, job_id=new_job.id, error_message="Modal refused the call."
            )
            read_household_job(session, new_job.id)

    expected_statements = 5
    assert orm_statements == [True] * expected_statements


def test_a_new_job_is_a_mapped_row_written_before_the_insert_returns(
    public_api_database_url,
) -> None:
    """The flush runs the INSERT at once, and its defaults outlive the session."""
    with rolled_back_transaction(public_api_database_url) as connection:
        new_job = _stored_job(connection)
        act_as_the_service(connection)

        with session_in(connection) as session:
            flushed = _flushed_rows(session)
            insert_household_job(session, new_job)
            written = connection.execute(
                text("SELECT count(*) FROM public.household_jobs WHERE id = :id"),
                {"id": new_job.id},
            ).scalar_one()

    assert [type(row) for row in flushed] == [HouseholdJob]
    job = flushed[0]
    assert job.id == new_job.id
    assert written == 1
    assert job.created_at is not None
    assert job.updated_at is not None


def test_a_job_read_in_the_session_that_changed_it_is_current(
    public_api_database_url,
) -> None:
    """The conditional updates run in the database; the read must see their result."""
    with rolled_back_transaction(public_api_database_url) as connection:
        new_job = _stored_job(connection)
        act_as_the_service(connection)

        with session_in(connection) as session:
            insert_household_job(session, new_job)
            record_household_call(session, job_id=new_job.id, modal_call_id="fc-1")
            recorded = read_household_job(session, new_job.id)
            connection.execute(
                text("RESET ROLE"),
            )
            connection.execute(
                text(
                    "UPDATE public.household_jobs SET status = 'running' WHERE id = :id"
                ),
                {"id": new_job.id},
            )
            act_as_the_service(connection)
            claimed = read_household_job(session, new_job.id)

    assert recorded is not None
    assert (recorded.status, recorded.modal_call_id) == (
        HouseholdJobStatus.PENDING,
        "fc-1",
    )
    assert claimed is not None
    assert claimed.status is HouseholdJobStatus.RUNNING


def test_a_household_simulation_run_is_written_before_anything_names_it(
    public_api_database_url,
) -> None:
    """The classes declare no relationships, so the flush can't order the rows."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        household_id = insert_household(
            connection, country="us", default_year=2026, document=DOCUMENT
        )
        report = insert_household_report_rows(
            connection,
            economy.catalog,
            household_id=household_id,
            year=2026,
            versions=VERSIONS,
            with_reform=False,
        )
        act_as_the_service(connection)
        run_id = uuid4()

        with session_in(connection) as session:
            flushed = _flushed_rows(session)
            insert_household_simulation_run(
                session,
                run_id=run_id,
                simulation_id=report.baseline.simulation_id,
                year=2026,
                tax_benefit_model_version_id=economy.catalog.version_id,
                versions=VERSIONS,
                forced=True,
            )
            forced = connection.execute(
                text("SELECT forced FROM public.simulation_runs WHERE id = :id"),
                {"id": run_id},
            ).scalar_one()

    assert [type(row) for row in flushed] == [SimulationRun]
    run = flushed[0]
    assert run.id == run_id
    assert forced is True
    assert run.created_at is not None


def test_a_report_run_losing_a_race_on_a_session_keeps_nothing_and_goes_on(
    public_api_database_url,
) -> None:
    """The savepoint takes back this call's runs; the session's transaction lives."""
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        household_id = insert_household(
            connection, country="us", default_year=2026, document=DOCUMENT
        )
        request = _keyed_report(economy.catalog, household_id)
        act_as_the_service(connection)
        first = in_session(connection, find_or_create_household_report, request)
        # With the first run failed, this call inserts a new simulation run.
        connection.execute(text("RESET ROLE"))
        connection.execute(
            text(
                "UPDATE public.simulation_runs SET status = 'failed', "
                "completed_at = now() WHERE id = :id"
            ),
            {"id": first.baseline.simulation_run_id},
        )
        act_as_the_service(connection)
        proposed_run_id = uuid4()

        with session_in(connection) as session:
            run, side_runs = find_or_insert_report_run_on_sides(
                session,
                {
                    "id": uuid4(),
                    "report_id": first.report_id,
                    "budget_window_run_id": None,
                    "year": None,
                    "country_package_version": VERSIONS.country_package_version,
                    "policyengine_version": VERSIONS.policyengine_version,
                    "status": ReportRunStatus.PENDING,
                    "modal_call_id": None,
                },
                {
                    "baseline": household_simulation_run_row(
                        run_id=proposed_run_id,
                        simulation_id=first.baseline.simulation_id,
                        year=2026,
                        tax_benefit_model_version_id=economy.catalog.version_id,
                        versions=VERSIONS,
                    )
                },
            )
            kept_runs = session.execute(
                select(func.count())
                .select_from(SimulationRun)
                .where(SimulationRun.id == proposed_run_id)
            ).scalar_one()

    assert (run.id, run.inserted) == (first.run.id, False)
    assert side_runs == {}
    assert kept_runs == 0


def test_an_economy_request_reads_its_catalog_version_on_its_own_connection(
    public_api_database_url,
) -> None:
    """Economy code still holds a connection, inside or outside a savepoint."""
    request = EconomyRequest(
        country="us",
        policyengine_version=POLICYENGINE_VERSION,
        region_code="us",
        dataset_name=None,
        year=2026,
    )
    with rolled_back_transaction(public_api_database_url) as connection:
        economy = insert_economy_catalog(connection)
        act_as_the_service(connection)

        in_transaction = read_catalog_rows(connection, request)
        with connection.begin_nested():
            in_savepoint = read_catalog_rows(connection, request)
        catalog_rows_still_visible = connection.execute(
            text("SELECT count(*) FROM public.datasets WHERE id = :id"),
            {"id": economy.dataset_id},
        ).scalar_one()

    assert in_transaction.catalog_version_id == economy.catalog.version_id
    assert in_savepoint.catalog_version_id == economy.catalog.version_id
    assert catalog_rows_still_visible == 1


def test_a_job_goes_through_its_sessions_and_stays_readable(
    committed_engine, committed_household, session_factory
) -> None:
    store = _job_store(session_factory)
    called = _new_job(committed_household.catalog, committed_household.household_id)
    refused = _new_job(committed_household.catalog, committed_household.household_id)

    store.create_job(called)
    store.create_job(refused)
    status = store.record_call(called.id, "fc-1")
    failed = store.fail_unstarted(refused.id, "Modal refused the call.")
    called_job = store.read(called.id)
    refused_job = store.read(refused.id)

    assert status is HouseholdJobStatus.PENDING
    assert failed is True
    assert called_job is not None
    assert called_job.modal_call_id == "fc-1"
    assert called_job.created_at is not None
    assert refused_job is not None
    assert refused_job.status is HouseholdJobStatus.FAILED
    assert refused_job.completed_at is not None
    assert _count(committed_engine, "household_jobs", called.id) == 1


def test_a_connection_lost_at_a_jobs_commit_leaves_the_outcome_unknown(
    committed_engine, committed_household, session_factory
) -> None:
    """The flush succeeded; PostgreSQL may have applied the commit."""
    unflushed_counts = _end_connection_at(
        session_factory, "before_commit", committed_engine
    )
    job = _new_job(committed_household.catalog, committed_household.household_id)

    with pytest.raises(EconomyRecordOutcomeUnknownError) as raised:
        _job_store(session_factory).create_job(job)

    assert isinstance(raised.value.__cause__, OperationalError)
    assert unflushed_counts == [0]


def test_a_connection_lost_during_a_jobs_flush_is_not_a_lost_commit(
    committed_engine, committed_household, session_factory
) -> None:
    unflushed_counts = _end_connection_at(
        session_factory, "before_flush", committed_engine
    )
    job = _new_job(committed_household.catalog, committed_household.household_id)

    with pytest.raises(OperationalError):
        _job_store(session_factory).create_job(job)

    assert unflushed_counts == [1]
    assert _count(committed_engine, "household_jobs", job.id) == 0


def test_a_flush_outlasting_the_statement_limit_is_not_a_lost_commit(
    committed_engine, committed_household, session_factory
) -> None:
    """The limit covers the flush, and a cancelled INSERT wrote nothing."""
    store = _job_store(
        session_factory,
        statement_timeout_milliseconds=SHORT_STATEMENT_TIMEOUT_MILLISECONDS,
    )
    job = _new_job(committed_household.catalog, committed_household.household_id)

    with committed_engine.begin() as locking_connection:
        locking_connection.execute(
            text("LOCK TABLE public.household_jobs IN ACCESS EXCLUSIVE MODE")
        )
        with pytest.raises(OperationalError, match="statement timeout"):
            store.create_job(job)

    assert _count(committed_engine, "household_jobs", job.id) == 0


def test_a_forced_rerun_commits_its_runs_through_a_session(
    committed_engine, committed_household, session_factory
) -> None:
    rerun = _forced_rerun(committed_engine, committed_household)

    created = _report_store(session_factory).create_report_rerun(rerun)

    assert (created.id, created.inserted) == (rerun.run_id, True)
    assert _count(committed_engine, "report_runs", rerun.run_id) == 1
    assert (
        _count(committed_engine, "simulation_runs", rerun.baseline.simulation_run_id)
        == 1
    )


def test_a_connection_lost_at_a_reruns_commit_leaves_the_outcome_unknown(
    committed_engine, committed_household, session_factory
) -> None:
    rerun = _forced_rerun(committed_engine, committed_household)
    unflushed_counts = _end_connection_at(
        session_factory, "before_commit", committed_engine
    )

    with pytest.raises(EconomyRecordOutcomeUnknownError) as raised:
        _report_store(session_factory).create_report_rerun(rerun)

    assert isinstance(raised.value.__cause__, OperationalError)
    assert unflushed_counts == [0]


def test_a_connection_lost_during_a_reruns_flush_is_not_a_lost_commit(
    committed_engine, committed_household, session_factory
) -> None:
    rerun = _forced_rerun(committed_engine, committed_household)
    _end_connection_at(session_factory, "before_flush", committed_engine)

    with pytest.raises(OperationalError):
        _report_store(session_factory).create_report_rerun(rerun)

    assert _count(committed_engine, "report_runs", rerun.run_id) == 0


def test_a_forced_rerun_naming_a_missing_simulation_writes_nothing(
    committed_engine, committed_household, session_factory
) -> None:
    """The flush itself is refused, which is no lost commit."""
    rerun = _forced_rerun(committed_engine, committed_household)
    missing_simulation = replace(
        rerun,
        baseline=HouseholdRerunSide(simulation_id=uuid4(), simulation_run_id=uuid4()),
    )

    with pytest.raises(EconomyRecordReferenceError) as refused:
        _report_store(session_factory).create_report_rerun(missing_simulation)

    assert (refused.value.table_name, refused.value.column_name) == (
        "simulation_runs",
        "simulation_id",
    )
    assert _count(committed_engine, "report_runs", rerun.run_id) == 0


def test_two_identical_report_requests_at_once_end_on_one_run(
    committed_engine, committed_household, session_factory
) -> None:
    """The first holds its session open until the second waits on its rows."""
    first_has_inserted = threading.Event()
    found: dict[str, Any] = {}

    def create(session: Session, name: str) -> None:
        act_as_the_service(session)
        found[name] = find_or_create_household_report(
            session,
            _keyed_report(
                committed_household.catalog, committed_household.household_id
            ),
        )

    def first_request() -> None:
        with session_factory.begin() as session:
            create(session, "first")
            holder_pid = session.execute(select(func.pg_backend_pid())).scalar_one()
            first_has_inserted.set()
            wait_until_another_backend_waits_for(committed_engine, holder_pid)

    def second_request() -> None:
        assert first_has_inserted.wait(LOCK_WAIT_TIMEOUT_SECONDS)
        with session_factory.begin() as session:
            create(session, "second")

    run_together((first_request, second_request), released=first_has_inserted)

    first, second = found["first"], found["second"]
    assert (second.report_id, second.run.id) == (first.report_id, first.run.id)
    assert (first.run.inserted, second.run.inserted) == (True, False)
    assert second.baseline == replace(first.baseline, run_inserted=False)
