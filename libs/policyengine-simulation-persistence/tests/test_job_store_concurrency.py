"""Two or more collectors, dispatchers or submitters acting on one job at once.

Result collection takes no lease: every write is a conditional update, so a
second writer changes nothing. These tests run against PostgreSQL. The first
group forces the exact interleaving (the second writer blocks on the first
writer's row lock, then re-checks its condition once the first commits); the
second group races many threads through the public store methods.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from job_store_support import new_job, running_job, runtime_store
from policyengine_simulation_persistence import normalized_database_url
from policyengine_simulation_persistence.jobs import JobStatus, job_results, jobs
from sqlalchemy import create_engine, func, select, text

pytestmark = pytest.mark.postgres

WRITERS = 8
BLOCK_TIMEOUT_SECONDS = 10


def _engine(database_url: str, application_name: str):
    return create_engine(
        normalized_database_url(database_url),
        connect_args={"application_name": application_name},
    )


def _wait_until_waiting_on_a_lock(observer, application_name: str) -> None:
    deadline = time.monotonic() + BLOCK_TIMEOUT_SECONDS
    query = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE application_name = :name AND wait_event_type = 'Lock'"
    )
    while time.monotonic() < deadline:
        with observer.connect() as connection:
            if connection.execute(query, {"name": application_name}).scalar():
                return
        time.sleep(0.02)
    pytest.fail(f"{application_name} never waited on the first writer's row lock")


class _Interleaving:
    """Hold writer A's transaction open while writer B runs into its lock."""

    def __init__(self, database) -> None:
        self.first = _engine(database.runtime_url, "first-writer")
        self.second = _engine(database.runtime_url, "second-writer")
        self.observer = _engine(database.superuser_url, "observer")

    def run(self, first_write, second_write, *, first_commits: bool = True):
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.first.connect() as first_connection:
                first_transaction = first_connection.begin()
                first_outcome = first_write(first_connection)

                def second() -> bool:
                    with self.second.begin() as second_connection:
                        return second_write(second_connection)

                second_future = executor.submit(second)
                _wait_until_waiting_on_a_lock(self.observer, "second-writer")
                if first_commits:
                    first_transaction.commit()
                else:
                    first_transaction.rollback()
            return first_outcome, second_future.result(timeout=BLOCK_TIMEOUT_SECONDS)

    def dispose(self) -> None:
        for engine in (self.first, self.second, self.observer):
            engine.dispose()


@pytest.fixture
def interleaving(migrated_database):
    controller = _Interleaving(migrated_database)
    yield controller
    controller.dispose()


def _stored_results(store, job_id):
    with store.engine.connect() as connection:
        return connection.execute(
            select(job_results.c.result).where(job_results.c.job_id == job_id)
        ).all()


# --- Forced interleavings ------------------------------------------------------


def test_a_second_collector_waits_for_the_first_then_changes_nothing(
    migrated_database, interleaving
) -> None:
    store = runtime_store(migrated_database)
    job = running_job(store)

    first, second = interleaving.run(
        lambda connection: store._complete(
            connection, job.id, result={"collector": "first"}, result_schema_version=1
        ),
        lambda connection: store._complete(
            connection, job.id, result={"collector": "second"}, result_schema_version=1
        ),
    )

    assert (first, second) == (True, False)
    assert store.get(job.id).status is JobStatus.SUCCEEDED
    assert _stored_results(store, job.id) == [({"collector": "first"},)]


def test_a_failure_racing_a_result_cannot_overwrite_it(
    migrated_database, interleaving
) -> None:
    store = runtime_store(migrated_database)
    job = running_job(store)

    first, second = interleaving.run(
        lambda connection: store._complete(
            connection, job.id, result={"collector": "first"}, result_schema_version=1
        ),
        lambda connection: store._fail(
            connection, job.id, error_code="worker_error", error_message="late"
        ),
    )

    assert (first, second) == (True, False)
    finished = store.get(job.id)
    assert finished.status is JobStatus.SUCCEEDED
    assert finished.error_code is None


def test_a_result_racing_a_failure_is_discarded(
    migrated_database, interleaving
) -> None:
    store = runtime_store(migrated_database)
    job = running_job(store)

    first, second = interleaving.run(
        lambda connection: store._fail(
            connection, job.id, error_code="result_expired", error_message="expired"
        ),
        lambda connection: store._complete(
            connection, job.id, result={"collector": "late"}, result_schema_version=1
        ),
    )

    assert (first, second) == (True, False)
    assert store.get(job.id).status is JobStatus.FAILED
    assert _stored_results(store, job.id) == []


def test_if_the_first_collector_rolls_back_the_second_writes_the_result(
    migrated_database, interleaving
) -> None:
    store = runtime_store(migrated_database)
    job = running_job(store)

    first, second = interleaving.run(
        lambda connection: store._complete(
            connection, job.id, result={"collector": "first"}, result_schema_version=1
        ),
        lambda connection: store._complete(
            connection, job.id, result={"collector": "second"}, result_schema_version=1
        ),
        first_commits=False,
    )

    assert (first, second) == (True, True)
    assert _stored_results(store, job.id) == [({"collector": "second"},)]


def test_a_second_dispatcher_waits_for_the_first_then_claims_nothing(
    migrated_database, interleaving
) -> None:
    store = runtime_store(migrated_database)
    job = store.submit(new_job()).job

    first, second = interleaving.run(
        lambda connection: store._claim_dispatch(connection, job.id),
        lambda connection: store._claim_dispatch(connection, job.id),
    )

    assert (first, second) == (True, False)
    assert store.get(job.id).status is JobStatus.DISPATCHING


# --- Many threads through the public methods ------------------------------------


def _race(action, count: int = WRITERS):
    barrier = threading.Barrier(count)

    def contender(index: int):
        barrier.wait()
        return action(index)

    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(contender, range(count)))


def test_concurrent_collectors_write_exactly_one_result(migrated_database) -> None:
    store = runtime_store(migrated_database)
    for case in range(5):
        job = running_job(store, {"case": case})

        outcomes = _race(
            lambda index, job_id=job.id: store.complete(
                job_id, result={"collector": index}, result_schema_version=1
            )
        )

        assert outcomes.count(True) == 1
        winner = outcomes.index(True)
        assert _stored_results(store, job.id) == [({"collector": winner},)]
        assert store.get(job.id).status is JobStatus.SUCCEEDED


def test_concurrent_results_and_failures_leave_one_consistent_outcome(
    migrated_database,
) -> None:
    store = runtime_store(migrated_database)
    for case in range(5):
        job = running_job(store, {"case": case})

        def collect(index, job_id=job.id):
            if index % 2:
                return store.fail(job_id, error_code="worker_error", error_message="x")
            return store.complete(
                job_id, result={"collector": index}, result_schema_version=1
            )

        outcomes = _race(collect)

        assert outcomes.count(True) == 1
        winner = outcomes.index(True)
        finished = store.get(job.id)
        if winner % 2:
            assert finished.status is JobStatus.FAILED
            assert _stored_results(store, job.id) == []
        else:
            assert finished.status is JobStatus.SUCCEEDED
            assert _stored_results(store, job.id) == [({"collector": winner},)]


def test_concurrent_dispatch_claims_start_one_modal_call(migrated_database) -> None:
    store = runtime_store(migrated_database)
    job = store.submit(new_job()).job

    outcomes = _race(lambda _index: store.claim_dispatch(job.id))

    assert outcomes.count(True) == 1


def test_concurrent_identical_submissions_create_one_job(migrated_database) -> None:
    store = runtime_store(migrated_database)

    submissions = _race(lambda _index: store.submit(new_job({"shared": True})))

    assert [submission.created for submission in submissions].count(True) == 1
    assert len({submission.job.id for submission in submissions}) == 1
    with store.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(jobs)).scalar() == 1


def test_concurrent_report_collectors_write_exactly_one_report_result(
    migrated_database,
) -> None:
    from policyengine_simulation_persistence.reports import (
        ReportKind,
        report_results,
    )
    from report_support import economy_aggregate

    store = runtime_store(migrated_database)
    for case in range(3):
        job = running_job(store, {"case": case})

        outcomes = _race(
            lambda index, job_id=job.id: store.complete_report(
                job_id,
                report_kind=ReportKind.ECONOMY,
                aggregate=economy_aggregate(model_version=f"collector-{index}"),
                aggregate_schema_version=1,
            )
        )

        assert outcomes.count(True) == 1
        with store.engine.connect() as connection:
            stored = connection.execute(
                select(report_results.c.model_version).where(
                    report_results.c.job_id == job.id
                )
            ).all()
        assert stored == [(f"collector-{outcomes.index(True)}",)]
