"""The simulation job service: submission, background collection and the sweep."""

from __future__ import annotations

import asyncio

import pytest
from policyengine_simulation_entry.simulation_jobs import (
    COLLECTION_STALE_AFTER,
    DISPATCH_TIMEOUT,
    FIRST_COLLECTION_DELAY_SECONDS,
    CallFailed,
    CallInspectionError,
    CallRunning,
    CallSucceeded,
    CollectionOutcome,
    CollectionQueueError,
    SimulationJobService,
    UnknownJobKindError,
    collection_delay_seconds,
)
from policyengine_simulation_persistence.jobs import JobStatus, input_sha256
from simulation_job_support import (
    HOUSEHOLD_KIND,
    TARGET,
    FakeDispatcher,
    FakeInspector,
    FakeQueue,
    InMemoryJobStore,
    SteppingClock,
)

REQUEST = {"household": {"people": {"you": {"age": 40}}}}
OBSERVABILITY_CONTEXT = {
    "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
}


def _service(
    *,
    dispatcher=None,
    inspector=None,
    queue=None,
    clock=None,
):
    clock = clock or SteppingClock()
    store = InMemoryJobStore(clock)
    service = SimulationJobService(
        store=store,
        dispatcher=dispatcher or FakeDispatcher(),
        inspector=inspector or FakeInspector(),
        queue=queue or FakeQueue(),
        job_kinds={HOUSEHOLD_KIND.name: HOUSEHOLD_KIND},
        clock=clock,
    )
    return service, store


async def _submit(service, request=REQUEST, submitted_by="api-v1@clients"):
    return await service.submit(
        kind=HOUSEHOLD_KIND.name,
        request_payload=request,
        policyengine_version="6.7.5",
        submitted_by=submitted_by,
        target=TARGET,
        observability_id="00000000-0000-4000-8000-000000000001",
        observability_context=OBSERVABILITY_CONTEXT,
    )


# --- Submission ----------------------------------------------------------------


def test_a_submission_records_the_job_dispatches_it_and_schedules_collection():
    dispatcher = FakeDispatcher()
    queue = FakeQueue()
    service, store = _service(dispatcher=dispatcher, queue=queue)

    job = asyncio.run(_submit(service))

    assert job.status is JobStatus.RUNNING
    assert job.modal_call_id == "fc-" + "1".zfill(26)
    assert job.submitted_by == "api-v1@clients"
    assert job.input_sha256 == input_sha256(
        kind=HOUSEHOLD_KIND.name, policyengine_version="6.7.5", request=REQUEST
    )
    assert (job.modal_application, job.modal_function) == (
        TARGET.application,
        TARGET.function,
    )
    assert dispatcher.spawned == [(TARGET, REQUEST, OBSERVABILITY_CONTEXT)]
    assert queue.enqueued == [(job.id, FIRST_COLLECTION_DELAY_SECONDS)]
    assert store.calls[:3] == ["submit", "claim_dispatch", "record_dispatched"]


def test_an_identical_submission_reuses_the_job_without_a_second_modal_call():
    dispatcher = FakeDispatcher()
    service, _ = _service(dispatcher=dispatcher)

    first = asyncio.run(_submit(service))
    second = asyncio.run(_submit(service, submitted_by="another@clients"))

    assert second.id == first.id
    assert len(dispatcher.spawned) == 1


def test_concurrent_identical_submissions_start_one_modal_call():
    dispatcher = FakeDispatcher()
    service, _ = _service(dispatcher=dispatcher)

    async def submit_together():
        return await asyncio.gather(*(_submit(service) for _ in range(5)))

    submitted = asyncio.run(submit_together())

    assert len({job.id for job in submitted}) == 1
    assert len(dispatcher.spawned) == 1


def test_a_refused_modal_call_fails_the_job_without_raising():
    dispatcher = FakeDispatcher(failure=ConnectionError("password=secret"))
    queue = FakeQueue()
    service, _ = _service(dispatcher=dispatcher, queue=queue)

    job = asyncio.run(_submit(service))

    assert job.status is JobStatus.FAILED
    assert job.error_code == "dispatch_failed"
    assert "secret" not in job.error_message
    assert queue.enqueued == []


def test_a_refused_collection_task_leaves_the_job_for_the_sweep():
    service, _ = _service(queue=FakeQueue(refuse=True))

    job = asyncio.run(_submit(service))

    assert job.status is JobStatus.RUNNING


def test_an_unregistered_kind_is_rejected_before_anything_is_written():
    service, store = _service()

    with pytest.raises(UnknownJobKindError):
        asyncio.run(
            service.submit(
                kind="unregistered",
                request_payload=REQUEST,
                policyengine_version="6.7.5",
                submitted_by="api-v1@clients",
                target=TARGET,
            )
        )

    assert store.jobs == {}


# --- Collection ----------------------------------------------------------------


def _running_job(service):
    return asyncio.run(_submit(service))


def test_a_running_call_is_counted_and_checked_again_with_backoff():
    inspector = FakeInspector(CallRunning())
    queue = FakeQueue()
    service, store = _service(inspector=inspector, queue=queue)
    job = _running_job(service)
    queue.enqueued.clear()

    first = asyncio.run(service.collect(job.id))
    second = asyncio.run(service.collect(job.id))

    assert first.outcome is CollectionOutcome.STILL_RUNNING
    assert (first.next_check_seconds, second.next_check_seconds) == (2, 4)
    assert queue.enqueued == [(job.id, 2), (job.id, 4)]
    assert store.get(job.id).collection_attempts == 2
    assert inspector.inspected == [job.modal_call_id, job.modal_call_id]


def test_the_backoff_doubles_up_to_a_minute():
    assert [collection_delay_seconds(attempt) for attempt in range(1, 9)] == [
        2,
        4,
        8,
        16,
        32,
        60,
        60,
        60,
    ]


def test_a_finished_call_stores_its_validated_result():
    inspector = FakeInspector(CallSucceeded({"net_income": 51234.5}))
    service, store = _service(inspector=inspector)
    job = _running_job(service)

    report = asyncio.run(service.collect(job.id))

    assert report.outcome is CollectionOutcome.SUCCEEDED
    assert store.get(job.id).status is JobStatus.SUCCEEDED
    assert store.get_result(job.id).result == {"net_income": 51234.5}
    assert store.get_result(job.id).result_schema_version == 1


def test_a_result_that_fails_validation_fails_the_job():
    inspector = FakeInspector(CallSucceeded({"net_income": "not a number"}))
    service, store = _service(inspector=inspector)
    job = _running_job(service)

    report = asyncio.run(service.collect(job.id))

    assert report.outcome is CollectionOutcome.FAILED
    assert store.get(job.id).error_code == "invalid_result"
    assert "not a number" not in store.get(job.id).error_message
    assert store.get_result(job.id) is None


def test_a_failed_call_fails_the_job():
    inspector = FakeInspector(CallFailed("worker_error", "The call raised KeyError."))
    service, store = _service(inspector=inspector)
    job = _running_job(service)

    report = asyncio.run(service.collect(job.id))

    assert report.outcome is CollectionOutcome.FAILED
    assert store.get(job.id).error_code == "worker_error"


def test_two_collectors_store_one_result_and_the_second_changes_nothing():
    service, store = _service()
    job = _running_job(service)

    class BothCollectorsSeeTheCallFinish:
        """Hold each collector until both have read the job as running."""

        def __init__(self):
            self.barrier = asyncio.Barrier(2)

        async def inspect(self, _modal_call_id):
            await self.barrier.wait()
            return CallSucceeded({"net_income": 1.0})

    async def collect_together():
        service.inspector = BothCollectorsSeeTheCallFinish()
        return await asyncio.gather(service.collect(job.id), service.collect(job.id))

    reports = asyncio.run(collect_together())

    assert sorted(report.outcome.value for report in reports) == sorted(
        [CollectionOutcome.SUCCEEDED.value, CollectionOutcome.ALREADY_FINISHED.value]
    )
    assert store.calls.count("complete") == 2
    assert list(store.results) == [job.id]
    assert store.get_result(job.id).result == {"net_income": 1.0}


def test_a_finished_job_is_never_inspected_again():
    inspector = FakeInspector(CallSucceeded({"net_income": 1.0}))
    service, _ = _service(inspector=inspector)
    job = _running_job(service)
    asyncio.run(service.collect(job.id))
    inspector.inspected.clear()

    report = asyncio.run(service.collect(job.id))

    assert report.outcome is CollectionOutcome.NOT_RUNNING
    assert report.job_status is JobStatus.SUCCEEDED
    assert inspector.inspected == []


def test_an_unknown_job_is_dropped():
    from uuid import uuid4

    service, _ = _service()

    report = asyncio.run(service.collect(uuid4()))

    assert report.outcome is CollectionOutcome.UNKNOWN_JOB


def test_a_job_of_an_unregistered_kind_fails_on_collection():
    inspector = FakeInspector(CallSucceeded({"net_income": 1.0}))
    service, store = _service(inspector=inspector)
    job = _running_job(service)
    service.job_kinds = {}

    report = asyncio.run(service.collect(job.id))

    assert report.outcome is CollectionOutcome.FAILED
    assert store.get(job.id).error_code == "unknown_job_kind"


@pytest.mark.parametrize(
    "failure",
    [CallInspectionError("Modal unavailable"), CollectionQueueError("refused")],
)
def test_transient_failures_propagate_so_cloud_tasks_retries(failure):
    if isinstance(failure, CallInspectionError):
        service, store = _service(inspector=FakeInspector(failure=failure))
    else:
        service, store = _service(queue=FakeQueue())
    job = _running_job(service)
    if isinstance(failure, CollectionQueueError):
        service.queue = FakeQueue(refuse=True)

    with pytest.raises(type(failure)):
        asyncio.run(service.collect(job.id))

    assert store.get(job.id).status is JobStatus.RUNNING


# --- The sweep -------------------------------------------------------------------


def test_the_sweep_enqueues_stale_running_jobs_and_fails_stalled_dispatches():
    clock = SteppingClock()
    queue = FakeQueue()
    service, store = _service(clock=clock, queue=queue)
    stale = _running_job(service)
    stalled = asyncio.run(
        asyncio.to_thread(store.submit, _new_job_like(stale, {"case": "stalled"}))
    ).job
    clock.advance(seconds=DISPATCH_TIMEOUT.total_seconds() + 1)
    fresh = asyncio.run(_submit(service, request={"case": "fresh"}))
    queue.enqueued.clear()

    report = asyncio.run(service.sweep())

    assert report.abandoned == 1
    assert store.get(stalled.id).status is JobStatus.FAILED
    assert report.enqueued == 1
    assert queue.enqueued == [(stale.id, 0)]
    assert fresh.id not in [job_id for job_id, _ in queue.enqueued]
    assert COLLECTION_STALE_AFTER < DISPATCH_TIMEOUT


def test_the_sweep_reports_refused_tasks():
    clock = SteppingClock()
    service, _ = _service(clock=clock)
    _running_job(service)
    clock.advance(seconds=COLLECTION_STALE_AFTER.total_seconds() + 1)
    service.queue = FakeQueue(refuse=True)

    report = asyncio.run(service.sweep())

    assert (report.enqueued, report.enqueue_failures) == (0, 1)


def _new_job_like(job, request):
    from policyengine_simulation_persistence.jobs import NewJob

    return NewJob(
        kind=job.kind,
        submitted_by=job.submitted_by,
        input_sha256=input_sha256(
            kind=job.kind, policyengine_version="6.7.5", request=request
        ),
        policyengine_version="6.7.5",
        modal_application=job.modal_application,
        modal_function=job.modal_function,
    )


# --- Status reads ----------------------------------------------------------------


def test_status_reads_accept_a_job_id_or_a_modal_call_id_and_never_call_modal():
    inspector = FakeInspector(CallSucceeded({"net_income": 1.0}))
    dispatcher = FakeDispatcher()
    service, _ = _service(inspector=inspector, dispatcher=dispatcher)
    job = _running_job(service)
    asyncio.run(service.collect(job.id))
    inspector.inspected.clear()

    by_id = asyncio.run(service.find(str(job.id)))
    by_call = asyncio.run(service.find(job.modal_call_id))

    assert by_id.job.id == by_call.job.id == job.id
    assert by_id.result.result == {"net_income": 1.0}
    assert inspector.inspected == []
    assert len(dispatcher.spawned) == 1


@pytest.mark.parametrize(
    "reference",
    ["not-a-job", "fc-", "fc-has spaces", "00000000-0000-4000-8000-000000000099"],
)
def test_unknown_or_malformed_references_find_nothing(reference):
    service, _ = _service()

    assert asyncio.run(service.find(reference)) is None


def test_an_unfinished_job_reads_without_a_result():
    service, _ = _service()
    job = _running_job(service)

    view = asyncio.run(service.find(str(job.id)))

    assert view.job.status is JobStatus.RUNNING
    assert view.result is None
