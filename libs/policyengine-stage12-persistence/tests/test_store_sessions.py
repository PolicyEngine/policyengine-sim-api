"""The run stores on ORM sessions, as the Modal worker role.

Each test runs a store on a real ``Session`` against a disposable Postgres
that API v1's v2 migrations created (see ``report_run_database``), and
the ``statements`` fixture fails it if a statement bypassed the session.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from economy_output import US_ECONOMY_OUTPUT
from household_report_database import (
    remove_household_report_run,
    seed_household_report_run,
)
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_simulation_persistence.result_tables import BudgetSummary, Poverty
from policyengine_stage12_persistence import (
    ActiveRun,
    HouseholdReportRunStore,
    ReportRunStore,
    RunKind,
    RunStatus,
    RunWrite,
    report_run_result,
)
from policyengine_stage12_persistence.report_run_results import ResultRows
from report_run_database import NOW
from session_database import (
    CONNECTION_REFUSED_MESSAGE,
    RetryingSessionOpener,
    limit_open_connections,
    refuse_connections,
)
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError, OperationalError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from household_report_database import SeededHouseholdReportRun
    from report_run_database import ReportRunSeeder, SeededHouseholdJob, SeededReportRun
    from session_database import StatementLog
    from sqlalchemy.orm import Session, sessionmaker

LATER = NOW + timedelta(minutes=5)
CUTOFF = NOW + timedelta(minutes=30)
CALL = "fc-session-call"
OTHER_CALL = "fc-other-call"
CONNECTION_CHECK_SENT = "SELECT true AS anon_1"
HOUSEHOLD_DOCUMENT = {"status": "ok", "values": [1, 2]}


@pytest.fixture
def store(
    worker_sessions: sessionmaker[Session], statements: StatementLog
) -> ReportRunStore:
    assert statements.sent == []
    return ReportRunStore(session_factory=worker_sessions)


@pytest.fixture
def household_store(
    worker_sessions: sessionmaker[Session], statements: StatementLog
) -> HouseholdReportRunStore:
    assert statements.sent == []
    return HouseholdReportRunStore(session_factory=worker_sessions)


@pytest.fixture
def household_report(
    seeder: ReportRunSeeder,
) -> Iterator[SeededHouseholdReportRun]:
    seeded = seed_household_report_run(seeder)
    try:
        yield seeded
    finally:
        remove_household_report_run(seeder, seeded)


def _result(run_ids: ReportRunIds):
    return report_run_result(US_ECONOMY_OUTPUT, run_ids=run_ids, country="us")


def _claimed(store: ReportRunStore, seeded_run: SeededReportRun) -> ReportRunIds:
    run_ids = seeded_run.run_ids
    store.claim_report_run(run_ids.report_run_id, modal_call_id=CALL, claimed_at=NOW)
    return run_ids


def test_a_refused_connection_is_opened_again_and_the_claim_runs_once(
    worker_sessions, statements, seeder, seeded_run
) -> None:
    attempts = refuse_connections(worker_sessions, count=1)
    opener = RetryingSessionOpener(statements)
    store = ReportRunStore(session_factory=worker_sessions, open_session=opener)
    report_run_id = seeded_run.run_ids.report_run_id

    write = store.claim_report_run(report_run_id, modal_call_id=CALL, claimed_at=NOW)

    assert write == RunWrite(changed=True, status=RunStatus.RUNNING)
    assert len(attempts) == 2
    assert CONNECTION_REFUSED_MESSAGE in str(opener.refusals[0])
    assert opener.operation_names == ["claim_report_run"]
    assert len(statements.starting_with("UPDATE")) == 1
    assert statements.commits == 1
    assert seeder.row("report_runs", report_run_id)["modal_call_id"] == CALL


def test_a_failed_connection_check_closes_its_session_before_the_retry(
    worker_sessions, statements, seeder, seeded_run, monkeypatch
) -> None:
    # With one connection allowed, the retry connects only if the first
    # session gave its connection back.
    limit_open_connections(worker_sessions, limit=1)
    dialect = worker_sessions.kw["bind"].dialect
    run_statement = dialect.do_execute
    lost_checks = [CONNECTION_CHECK_SENT]

    def lose_the_first_check(cursor, statement, parameters, context=None) -> None:
        if lost_checks and statement == lost_checks[0]:
            lost_checks.clear()
            message = "server closed the connection unexpectedly"
            raise dialect.loaded_dbapi.OperationalError(message)
        run_statement(cursor, statement, parameters, context)

    monkeypatch.setattr(dialect, "do_execute", lose_the_first_check)
    opener = RetryingSessionOpener(statements)
    store = ReportRunStore(session_factory=worker_sessions, open_session=opener)
    report_run_id = seeded_run.run_ids.report_run_id

    write = store.claim_report_run(report_run_id, modal_call_id=CALL, claimed_at=NOW)

    assert write == RunWrite(changed=True, status=RunStatus.RUNNING)
    assert len(opener.refusals) == 1
    assert len(statements.starting_with("UPDATE")) == 1
    assert seeder.row("report_runs", report_run_id)["modal_call_id"] == CALL


def test_only_the_connection_check_runs_while_a_session_opens(
    worker_sessions, statements, seeded_run
) -> None:
    opener = RetryingSessionOpener(statements)
    store = ReportRunStore(session_factory=worker_sessions, open_session=opener)

    store.claim_report_run(
        seeded_run.run_ids.report_run_id, modal_call_id=CALL, claimed_at=NOW
    )

    assert opener.sent_while_opening == [[CONNECTION_CHECK_SENT]]
    assert statements.sent[0] == CONNECTION_CHECK_SENT


def test_a_lost_commit_reply_is_raised_and_the_write_is_not_repeated(
    worker_sessions, statements, seeder, seeded_run, monkeypatch
) -> None:
    dialect = worker_sessions.kw["bind"].dialect

    def commit_then_lose_the_reply(dbapi_connection) -> None:
        dbapi_connection.commit()
        message = "server closed the connection unexpectedly"
        raise dialect.loaded_dbapi.OperationalError(message)

    monkeypatch.setattr(dialect, "do_commit", commit_then_lose_the_reply)
    opener = RetryingSessionOpener(statements)
    store = ReportRunStore(session_factory=worker_sessions, open_session=opener)
    report_run_id = seeded_run.run_ids.report_run_id

    with pytest.raises(OperationalError, match="server closed the connection"):
        store.claim_report_run(report_run_id, modal_call_id=CALL, claimed_at=NOW)

    assert opener.operation_names == ["claim_report_run"]
    assert opener.refusals == []
    assert len(statements.starting_with("UPDATE")) == 1
    # The commit reached the database: only its reply was lost.
    assert seeder.row("report_runs", report_run_id)["status"] == "running"


def test_a_statement_failure_after_opening_reaches_no_opener(
    worker_sessions, statements, seeded_run
) -> None:
    opener = RetryingSessionOpener(statements)
    store = ReportRunStore(session_factory=worker_sessions, open_session=opener)
    run_ids = _claimed(store, seeded_run)
    result = _result(run_ids)
    budget = next(rows for rows in result.rows if rows.model is BudgetSummary)
    duplicated = ResultRows(
        model=BudgetSummary,
        rows=(*budget.rows, {**budget.rows[0], "id": uuid4()}),
        shared_by_reports=False,
    )
    broken = replace(result, rows=(duplicated,))

    with pytest.raises(IntegrityError):
        store.complete_report_run(
            run_ids, modal_call_id=CALL, completed_at=LATER, result=broken
        )

    assert opener.operation_names == ["claim_report_run", "complete_report_run"]
    assert opener.refusals == []


def test_each_operation_commits_its_own_session(store, statements, seeded_run) -> None:
    _claimed(store, seeded_run)
    assert statements.commits == 1
    store.get_simulation_run(seeded_run.run_ids.baseline_simulation_run_id)
    assert statements.commits == 2


def test_a_conditional_update_returns_only_the_status_it_reads(
    store, statements, seeded_run
) -> None:
    _claimed(store, seeded_run)

    [claim] = statements.starting_with("UPDATE")
    assert claim.startswith("UPDATE public.report_runs SET ")
    assert claim.endswith("RETURNING public.report_runs.status")
    assert statements.starting_with("SELECT public.") == []


def test_concurrent_claims_leave_the_run_with_one_call(
    worker_sessions, statements, seeder, seeded_run
) -> None:
    assert statements.sent == []
    report_run_id = seeded_run.run_ids.report_run_id
    claimants = 6
    start_together = Barrier(claimants)

    def claim(claimant: int) -> tuple[str, RunWrite]:
        store = ReportRunStore(session_factory=worker_sessions)
        call = f"fc-claimant-{claimant}"
        start_together.wait(timeout=10)
        return call, store.claim_report_run(
            report_run_id, modal_call_id=call, claimed_at=NOW
        )

    with ThreadPoolExecutor(max_workers=claimants) as executor:
        outcomes = list(executor.map(claim, range(claimants)))

    winners = [call for call, write in outcomes if write.changed]
    assert len(winners) == 1
    assert seeder.row("report_runs", report_run_id)["modal_call_id"] == winners[0]
    assert all(write.status is RunStatus.RUNNING for _call, write in outcomes)


def test_a_report_run_names_its_simulation_runs(store, seeded_run) -> None:
    run_ids = seeded_run.run_ids
    store.require_simulation_runs(run_ids)

    other = ReportRunIds(
        report_run_id=run_ids.report_run_id,
        baseline_simulation_run_id=run_ids.reform_simulation_run_id,
        reform_simulation_run_id=run_ids.baseline_simulation_run_id,
    )
    with pytest.raises(ValueError, match="names other simulation runs"):
        store.require_simulation_runs(other)
    missing = other.model_copy(update={"report_run_id": uuid4()})
    with pytest.raises(LookupError, match="does not exist"):
        store.require_simulation_runs(missing)


def test_a_simulation_run_is_read_as_plain_values(store, seeded_run) -> None:
    simulation_run_id = seeded_run.run_ids.baseline_simulation_run_id

    state = store.get_simulation_run(simulation_run_id)

    assert state.status is RunStatus.PENDING
    assert state.year == 2026
    assert state.modal_call_id is None
    with pytest.raises(LookupError, match="does not exist"):
        store.get_simulation_run(uuid4())


def test_a_completion_stores_results_once_with_null_columns_null(
    store, seeder, seeded_run
) -> None:
    run_ids = _claimed(store, seeded_run)
    result = _result(run_ids)

    first = store.complete_report_run(
        run_ids, modal_call_id=CALL, completed_at=LATER, result=result
    )
    repeated = store.complete_report_run(
        run_ids, modal_call_id=CALL, completed_at=LATER, result=_result(run_ids)
    )

    assert first == RunWrite(changed=True, status=RunStatus.SUCCEEDED)
    assert repeated == RunWrite(changed=False, status=RunStatus.SUCCEEDED)
    baseline = run_ids.baseline_simulation_run_id
    stored = seeder.rows("poverty", simulation_run_id=baseline)
    expected = next(rows for rows in result.rows if rows.model is Poverty)
    baseline_rows = [
        row for row in expected.rows if row["simulation_run_id"] == baseline
    ]
    assert len(stored) == len(baseline_rows)
    assert [row for row in stored if row["filter_variable"] is None]
    budget = seeder.rows("budget_summary", report_run_id=run_ids.report_run_id)
    assert budget
    assert seeder.row("report_runs", run_ids.report_run_id)["status"] == "succeeded"


def test_a_failed_result_insert_leaves_the_run_running_without_results(
    store, seeder, seeded_run
) -> None:
    run_ids = _claimed(store, seeded_run)
    result = _result(run_ids)
    budget = next(rows for rows in result.rows if rows.model is BudgetSummary)
    duplicated = ResultRows(
        model=BudgetSummary,
        rows=(*budget.rows, {**budget.rows[0], "id": uuid4()}),
        shared_by_reports=False,
    )
    other_rows = tuple(rows for rows in result.rows if rows is not budget)

    with pytest.raises(IntegrityError):
        store.complete_report_run(
            run_ids,
            modal_call_id=CALL,
            completed_at=LATER,
            result=replace(result, rows=(*other_rows, duplicated)),
        )

    assert seeder.row("report_runs", run_ids.report_run_id)["status"] == "running"
    assert seeder.rows("decile_impacts", report_run_id=run_ids.report_run_id) == []
    baseline = run_ids.baseline_simulation_run_id
    assert seeder.rows("poverty", simulation_run_id=baseline) == []


def test_a_shared_simulation_run_keeps_its_stored_result_row(
    store, seeder, seeded_run
) -> None:
    run_ids = _claimed(store, seeded_run)
    baseline = run_ids.baseline_simulation_run_id
    with seeder.engine.begin() as connection:
        connection.execute(
            insert(seeder.table("poverty")).values(
                id=uuid4(),
                simulation_run_id=baseline,
                poverty_type="spm",
                entity="person",
                filter_variable=None,
                filter_group=None,
                rate=0.5,
            )
        )

    store.complete_report_run(
        run_ids, modal_call_id=CALL, completed_at=LATER, result=_result(run_ids)
    )

    stored = seeder.rows(
        "poverty",
        simulation_run_id=baseline,
        poverty_type="spm",
        filter_variable=None,
    )
    assert [row["rate"] for row in stored] == [0.5]


def test_a_report_run_fails_only_under_its_call(store, seeder, seeded_run) -> None:
    run_ids = _claimed(store, seeded_run)
    report_run_id = run_ids.report_run_id

    refused = store.fail_report_run(
        report_run_id, modal_call_id=OTHER_CALL, error_message="no", failed_at=LATER
    )
    failed = store.fail_report_run(
        report_run_id, modal_call_id=CALL, error_message="it broke", failed_at=LATER
    )

    assert refused == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    assert seeder.row("report_runs", report_run_id)["error_message"] == "it broke"
    with pytest.raises(LookupError, match=r"report run .* does not exist"):
        store.fail_report_run(
            uuid4(), modal_call_id=CALL, error_message="x", failed_at=LATER
        )


def test_a_simulation_run_is_claimed_and_completed_under_its_call(
    store, seeder, seeded_run
) -> None:
    simulation_run_id = seeded_run.run_ids.baseline_simulation_run_id

    claim = store.claim_simulation_run(
        simulation_run_id, modal_call_id=CALL, claimed_at=NOW
    )
    taken = store.claim_simulation_run(
        simulation_run_id, modal_call_id=OTHER_CALL, claimed_at=LATER
    )
    completed = store.complete_simulation_run(
        simulation_run_id,
        modal_call_id=CALL,
        output_uri="gs://bucket/output",
        output_sha256="a" * 64,
        completed_at=LATER,
    )

    assert claim.changed
    assert taken == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert completed == RunWrite(changed=True, status=RunStatus.SUCCEEDED)
    stored = seeder.row("simulation_runs", simulation_run_id)
    assert stored["output_uri"] == "gs://bucket/output"
    assert stored["started_at"] == NOW


@pytest.mark.parametrize("names_the_call", [True, False])
def test_a_simulation_run_is_failed_with_or_without_its_call(
    store, seeder, seeded_run, names_the_call
) -> None:
    simulation_run_id = seeded_run.run_ids.reform_simulation_run_id
    store.claim_simulation_run(simulation_run_id, modal_call_id=CALL, claimed_at=NOW)

    failed = store.fail_simulation_run(
        simulation_run_id,
        error_message="it broke",
        failed_at=LATER,
        modal_call_id=CALL if names_the_call else None,
    )

    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    stored = seeder.row("simulation_runs", simulation_run_id)
    assert stored["error_message"] == "it broke"


def test_a_run_held_by_an_ended_call_is_failed_once(store, seeded_run) -> None:
    simulation_run_id = seeded_run.run_ids.baseline_simulation_run_id
    store.claim_simulation_run(simulation_run_id, modal_call_id=CALL, claimed_at=NOW)

    first, second = (
        store.fail_simulation_run_held_by(
            simulation_run_id,
            modal_call_id=CALL,
            error_message="its call ended",
            failed_at=LATER,
        )
        for _ in range(2)
    )

    assert first == RunWrite(changed=True, status=RunStatus.FAILED)
    assert second == RunWrite(changed=False, status=RunStatus.FAILED)


def test_a_household_job_is_claimed_and_completed_with_its_document(
    store, seeder, seeded_household_job: SeededHouseholdJob
) -> None:
    job_id = seeded_household_job.job_id

    claim = store.claim_household_job(job_id, modal_call_id=CALL, claimed_at=NOW)
    completed = store.complete_household_job(
        job_id,
        modal_call_id=CALL,
        result=HOUSEHOLD_DOCUMENT,
        completed_at=LATER,
    )

    assert claim.changed
    assert completed == RunWrite(changed=True, status=RunStatus.SUCCEEDED)
    assert seeder.row("household_jobs", job_id)["result"] == HOUSEHOLD_DOCUMENT


def test_a_household_job_failed_without_a_document_stores_sql_null(
    store, seeder, seeded_household_job: SeededHouseholdJob
) -> None:
    job_id = seeded_household_job.job_id
    store.claim_household_job(job_id, modal_call_id=CALL, claimed_at=NOW)

    failed = store.fail_household_job(
        job_id, modal_call_id=CALL, error_message="it broke", failed_at=LATER
    )

    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    jobs = seeder.table("household_jobs")
    with seeder.engine.connect() as connection:
        result_is_null = connection.execute(
            select(jobs.c.result.is_(None)).where(jobs.c.id == job_id)
        ).scalar_one()
    assert result_is_null


@pytest.mark.parametrize("regardless_of_report_runs", [False, True])
def test_the_sweep_lists_and_fails_a_stuck_report_run_once(
    store, seeder, seeded_run, regardless_of_report_runs
) -> None:
    report_run_id = _claimed(store, seeded_run).report_run_id

    [listed] = [
        run
        for run in store.list_active_runs(
            RunKind.REPORT_RUN,
            active_since_before=CUTOFF,
            regardless_of_report_runs=regardless_of_report_runs,
        )
        if run.run_id == report_run_id
    ]
    first, second = (
        store.fail_stuck_run(
            listed,
            error_message="stuck",
            failed_at=LATER,
            regardless_of_report_runs=regardless_of_report_runs,
        )
        for _ in range(2)
    )

    assert listed == ActiveRun(
        kind=RunKind.REPORT_RUN,
        run_id=report_run_id,
        status=RunStatus.RUNNING,
        modal_call_id=CALL,
    )
    assert first == RunWrite(changed=True, status=RunStatus.FAILED)
    assert second == RunWrite(changed=False, status=RunStatus.FAILED)
    assert seeder.row("report_runs", report_run_id)["error_message"] == "stuck"


def test_concurrent_sweeps_fail_a_stuck_household_job_once(
    store, worker_sessions, seeded_household_job: SeededHouseholdJob
) -> None:
    job_id = seeded_household_job.job_id
    store.claim_household_job(job_id, modal_call_id=CALL, claimed_at=NOW)
    [listed] = [
        run
        for run in store.list_active_runs(
            RunKind.HOUSEHOLD_JOB, active_since_before=NOW + timedelta(days=36500)
        )
        if run.run_id == job_id
    ]
    sweeps = 4
    start_together = Barrier(sweeps)

    def sweep(_sweep_number: int) -> RunWrite:
        sweeping_store = ReportRunStore(session_factory=worker_sessions)
        start_together.wait(timeout=10)
        return sweeping_store.fail_stuck_run(
            listed, error_message="stuck", failed_at=LATER
        )

    with ThreadPoolExecutor(max_workers=sweeps) as executor:
        writes = list(executor.map(sweep, range(sweeps)))

    assert sum(write.changed for write in writes) == 1


def test_the_sweep_waits_for_report_runs_naming_a_simulation_run(
    store, seeder, seeded_run
) -> None:
    run_ids = seeded_run.run_ids
    baseline = run_ids.baseline_simulation_run_id
    store.claim_simulation_run(baseline, modal_call_id=CALL, claimed_at=NOW)

    def listed_ids(*, regardless_of_report_runs: bool) -> set:
        return {
            run.run_id
            for run in store.list_active_runs(
                RunKind.SIMULATION_RUN,
                active_since_before=CUTOFF,
                regardless_of_report_runs=regardless_of_report_runs,
            )
        }

    assert baseline not in listed_ids(regardless_of_report_runs=False)
    assert baseline in listed_ids(regardless_of_report_runs=True)
    named = ActiveRun(
        kind=RunKind.SIMULATION_RUN,
        run_id=baseline,
        status=RunStatus.RUNNING,
        modal_call_id=CALL,
    )
    left_alone = store.fail_stuck_run(named, error_message="stuck", failed_at=LATER)
    failed = store.fail_stuck_run(
        named,
        error_message="stuck",
        failed_at=LATER,
        regardless_of_report_runs=True,
    )

    assert left_alone == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    assert seeder.row("simulation_runs", baseline)["error_message"] == "stuck"


def test_a_household_report_run_records_each_side_under_its_call(
    household_store, seeder, household_report
) -> None:
    report_run_id = household_report.report_run_id
    baseline = household_report.baseline_simulation_run_id
    reform = household_report.reform_simulation_run_id

    named = household_store.household_report_simulation_runs(report_run_id)
    household_store.claim_report_run(report_run_id, modal_call_id=CALL, claimed_at=NOW)
    for simulation_run_id in (baseline, reform):
        household_store.claim_simulation_run(
            simulation_run_id, modal_call_id=CALL, claimed_at=NOW
        )
    completed = household_store.complete_household_simulation_run(
        baseline,
        modal_call_id=CALL,
        household_result=HOUSEHOLD_DOCUMENT,
        completed_at=LATER,
    )
    failed = household_store.fail_household_simulation_run(
        reform, modal_call_id=CALL, error_message="it broke", failed_at=LATER
    )
    report = household_store.complete_household_report_run(
        report_run_id, modal_call_id=CALL, completed_at=LATER
    )

    assert named == (baseline, reform)
    assert completed == RunWrite(changed=True, status=RunStatus.SUCCEEDED)
    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    assert report == RunWrite(changed=True, status=RunStatus.SUCCEEDED)
    stored = seeder.row("simulation_runs", baseline)
    assert stored["household_result"] == HOUSEHOLD_DOCUMENT
    with pytest.raises(LookupError, match="does not exist"):
        household_store.household_report_simulation_runs(uuid4())
