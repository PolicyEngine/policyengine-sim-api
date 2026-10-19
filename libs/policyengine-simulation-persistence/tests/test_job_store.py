"""The simulation.jobs store: idempotent submission and conditional transitions."""

from __future__ import annotations

from uuid import uuid4

import pytest
from job_store_support import (
    START,
    SteppingClock,
    new_job,
    running_job,
    runtime_store,
)
from policyengine_simulation_persistence.jobs import (
    JobStatus,
    input_sha256,
    result_sha256,
)

RESULT = {"net_income": 51234.5, "programs": {"snap": 0}}


# --- Hashing, without a database -------------------------------------------


def test_the_input_hash_ignores_key_order_but_covers_kind_and_bundle() -> None:
    first = input_sha256(
        kind="household_calculation",
        policyengine_version="6.7.5",
        request={"b": 1, "a": {"y": 2, "x": 3}},
    )

    assert first == input_sha256(
        kind="household_calculation",
        policyengine_version="6.7.5",
        request={"a": {"x": 3, "y": 2}, "b": 1},
    )
    assert first != input_sha256(
        kind="household_calculation",
        policyengine_version="6.7.6",
        request={"b": 1, "a": {"y": 2, "x": 3}},
    )
    assert first != input_sha256(
        kind="economy_report",
        policyengine_version="6.7.5",
        request={"b": 1, "a": {"y": 2, "x": 3}},
    )
    assert len(first) == 64


def test_the_result_hash_is_canonical() -> None:
    assert result_sha256({"b": 1, "a": 2}) == result_sha256({"a": 2, "b": 1})


# --- Submission and dispatch ------------------------------------------------


@pytest.mark.postgres
def test_identical_submissions_share_one_job(migrated_database) -> None:
    store = runtime_store(migrated_database)

    first = store.submit(new_job())
    second = store.submit(new_job(submitted_by="another-client@clients"))

    assert first.created is True
    assert second.created is False
    assert second.job.id == first.job.id
    assert second.job.submitted_by == "api-v1-client@clients"
    assert first.job.status is JobStatus.PENDING
    assert first.job.created_at == START


@pytest.mark.postgres
def test_a_failed_job_lets_the_same_input_be_submitted_again(migrated_database) -> None:
    store = runtime_store(migrated_database)
    failed = running_job(store)
    assert store.fail(failed.id, error_code="worker_error", error_message="boom")

    retried = store.submit(new_job())

    assert retried.created is True
    assert retried.job.id != failed.id
    assert store.get(failed.id).status is JobStatus.FAILED


@pytest.mark.postgres
def test_dispatch_is_claimed_once_and_records_the_modal_call(migrated_database) -> None:
    clock = SteppingClock()
    store = runtime_store(migrated_database, clock)
    job = store.submit(new_job()).job

    assert store.claim_dispatch(job.id) is True
    assert store.claim_dispatch(job.id) is False
    clock.advance(seconds=2)
    assert store.record_dispatched(job.id, modal_call_id="fc-01ABC") is True
    assert store.record_dispatched(job.id, modal_call_id="fc-other") is False

    dispatched = store.get(job.id)
    assert dispatched.status is JobStatus.RUNNING
    assert dispatched.modal_call_id == "fc-01ABC"
    assert dispatched.dispatched_at == clock.now
    assert store.get_by_modal_call_id("fc-01ABC").id == job.id
    assert store.get_by_modal_call_id("fc-unknown") is None


@pytest.mark.postgres
def test_a_rejected_dispatch_fails_the_job(migrated_database) -> None:
    store = runtime_store(migrated_database)
    job = store.submit(new_job()).job
    assert store.claim_dispatch(job.id)

    assert store.record_dispatch_failure(
        job.id, error_code="dispatch_failed", error_message="Modal refused"
    )

    failed = store.get(job.id)
    assert failed.status is JobStatus.FAILED
    assert failed.error_code == "dispatch_failed"
    assert failed.completed_at is not None


@pytest.mark.postgres
def test_an_unknown_job_reads_as_none(migrated_database) -> None:
    store = runtime_store(migrated_database)

    assert store.get(uuid4()) is None
    assert store.get_result(uuid4()) is None
    assert store.claim_dispatch(uuid4()) is False


# --- Collection ---------------------------------------------------------------


@pytest.mark.postgres
def test_a_result_is_written_once_with_its_hash(migrated_database) -> None:
    clock = SteppingClock()
    store = runtime_store(migrated_database, clock)
    job = running_job(store)
    clock.advance(minutes=3)

    assert store.complete(job.id, result=RESULT, result_schema_version=1) is True
    assert (
        store.complete(job.id, result={"other": True}, result_schema_version=1) is False
    )

    finished = store.get(job.id)
    stored = store.get_result(job.id)
    assert finished.status is JobStatus.SUCCEEDED
    assert finished.completed_at == clock.now
    assert stored.result == RESULT
    assert stored.result_schema_version == 1
    assert stored.result_sha256 == result_sha256(RESULT)


@pytest.mark.postgres
def test_only_a_running_job_can_finish(migrated_database) -> None:
    store = runtime_store(migrated_database)
    pending = store.submit(new_job()).job

    assert store.complete(pending.id, result=RESULT, result_schema_version=1) is False
    assert store.fail(pending.id, error_code="x", error_message="y") is False
    assert store.get(pending.id).status is JobStatus.PENDING
    assert store.get_result(pending.id) is None


@pytest.mark.postgres
def test_a_failure_cannot_overwrite_a_result(migrated_database) -> None:
    store = runtime_store(migrated_database)
    job = running_job(store)
    assert store.complete(job.id, result=RESULT, result_schema_version=1)

    assert store.fail(job.id, error_code="worker_error", error_message="late") is False

    assert store.get(job.id).status is JobStatus.SUCCEEDED
    assert store.get(job.id).error_code is None


@pytest.mark.postgres
def test_long_error_messages_are_truncated(migrated_database) -> None:
    store = runtime_store(migrated_database)
    job = running_job(store)

    assert store.fail(job.id, error_code="worker_error", error_message="x" * 10_000)

    assert len(store.get(job.id).error_message) == 2000


@pytest.mark.postgres
def test_each_check_of_a_running_job_is_counted(migrated_database) -> None:
    clock = SteppingClock()
    store = runtime_store(migrated_database, clock)
    job = running_job(store)

    clock.advance(seconds=5)
    assert store.record_check(job.id) == 1
    assert store.record_check(job.id) == 2
    assert store.get(job.id).last_checked_at == clock.now
    assert store.complete(job.id, result=RESULT, result_schema_version=1)
    assert store.record_check(job.id) is None


# --- The sweep ------------------------------------------------------------------


@pytest.mark.postgres
def test_the_sweep_selects_running_jobs_not_checked_recently(migrated_database) -> None:
    clock = SteppingClock()
    store = runtime_store(migrated_database, clock)
    never_checked = running_job(store, {"case": 1})
    checked_long_ago = running_job(store, {"case": 2})
    store.record_check(checked_long_ago.id)
    clock.advance(minutes=10)
    checked_recently = running_job(store, {"case": 3})
    store.record_check(checked_recently.id)
    finished = running_job(store, {"case": 4})
    store.complete(finished.id, result=RESULT, result_schema_version=1)
    pending = store.submit(new_job({"case": 5})).job

    selected = store.jobs_to_collect(
        checked_before=clock.now - _minutes(5),
        limit=10,
    )

    assert set(selected) == {never_checked.id, checked_long_ago.id}
    assert pending.id not in selected
    assert len(store.jobs_to_collect(checked_before=clock.now, limit=1)) == 1


@pytest.mark.postgres
def test_the_sweep_fails_dispatches_that_stalled(migrated_database) -> None:
    clock = SteppingClock()
    store = runtime_store(migrated_database, clock)
    stalled_pending = store.submit(new_job({"case": "pending"})).job
    stalled_dispatch = store.submit(new_job({"case": "dispatching"})).job
    store.claim_dispatch(stalled_dispatch.id)
    running = running_job(store, {"case": "running"})
    clock.advance(minutes=15)
    recent = store.submit(new_job({"case": "recent"})).job

    abandoned = store.abandon_stalled_dispatches(
        updated_before=clock.now - _minutes(10)
    )

    assert set(abandoned) == {stalled_pending.id, stalled_dispatch.id}
    for job_id in abandoned:
        assert store.get(job_id).status is JobStatus.FAILED
        assert store.get(job_id).error_code == "dispatch_abandoned"
    assert store.get(recent.id).status is JobStatus.PENDING
    assert store.get(running.id).status is JobStatus.RUNNING


def _minutes(count):
    from datetime import timedelta

    return timedelta(minutes=count)
