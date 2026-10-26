"""Starting a run's Modal call exactly once, and reading Modal's answers."""

from __future__ import annotations

import asyncio
import builtins

import grpclib.const
import grpclib.exceptions
import modal
import pytest
from modal._serialization import pickle_traceback, serialize
from modal._utils import blob_utils
from modal._utils.function_utils import _process_result
from modal_proto import api_pb2
from policyengine_simulation_entry import modal_calls
from policyengine_simulation_entry.modal_calls import (
    CallFailed,
    CallInspectionError,
    CallRunning,
    CallSucceeded,
    ModalCallDispatcher,
    ModalCallInspector,
)
from policyengine_simulation_entry.run_dispatch import (
    FIRST_CHECK_DELAY,
    DispatchOutcome,
    RunDispatchService,
)
from policyengine_simulation_persistence import RunError, RunErrorCode, RunStatus
from run_fakes import (
    SAMPLE_REQUEST,
    FakeDispatcher,
    FakeRunTransitions,
    ManualClock,
    execution,
)


def _service(dispatcher=None):
    clock = ManualClock()
    transitions = FakeRunTransitions(clock)
    service = RunDispatchService(
        transitions=transitions,
        dispatcher=dispatcher or FakeDispatcher(),
        clock=clock,
    )
    return service, transitions, clock


@pytest.mark.asyncio
async def test_a_claimed_run_starts_its_call_and_records_it() -> None:
    service, transitions, clock = _service()
    run_id = transitions.add_run()

    outcome = await service.start_simulation_runs([run_id], SAMPLE_REQUEST)

    assert outcome is DispatchOutcome.STARTED
    run = execution(transitions, run_id)
    assert run.status is RunStatus.RUNNING
    assert run.modal_call_id == "fc-1"
    assert run.next_check_at == clock() + FIRST_CHECK_DELAY
    assert service.dispatcher.requests == [SAMPLE_REQUEST]


@pytest.mark.asyncio
async def test_concurrent_starts_spawn_one_call() -> None:
    service, transitions, _ = _service()
    run_id = transitions.add_run()

    outcomes = await asyncio.gather(
        *(service.start_simulation_runs([run_id], SAMPLE_REQUEST) for _ in range(5))
    )

    assert outcomes.count(DispatchOutcome.STARTED) == 1
    assert outcomes.count(DispatchOutcome.NOT_CLAIMED) == 4
    assert len(service.dispatcher.requests) == 1


@pytest.mark.asyncio
async def test_a_household_pair_starts_one_shared_call() -> None:
    service, transitions, _ = _service()
    pair = [transitions.add_run("household"), transitions.add_run("household")]

    outcome = await service.start_simulation_runs(pair, SAMPLE_REQUEST)

    assert outcome is DispatchOutcome.STARTED
    assert {execution(transitions, run_id).modal_call_id for run_id in pair} == {"fc-1"}


@pytest.mark.asyncio
async def test_a_refused_call_fails_the_runs_without_raising() -> None:
    service, transitions, _ = _service(
        FakeDispatcher(failure=modal.exception.NotFoundError("no such function"))
    )
    run_id = transitions.add_run()

    outcome = await service.start_simulation_runs([run_id], SAMPLE_REQUEST)

    assert outcome is DispatchOutcome.DISPATCH_FAILED
    run = execution(transitions, run_id)
    assert run.status is RunStatus.FAILED
    assert run.error.code is RunErrorCode.DISPATCH_FAILED
    assert "NotFoundError" in run.error.message
    assert "no such function" not in run.error.message


@pytest.mark.asyncio
async def test_a_call_whose_runs_stopped_dispatching_is_cancelled(caplog) -> None:
    service, transitions, _ = _service()
    run_id = transitions.add_run()

    class SlowDispatcher(FakeDispatcher):
        async def spawn(self, request):
            transitions.runs[run_id].status = RunStatus.FAILED
            return await super().spawn(request)

    service.dispatcher = SlowDispatcher()

    outcome = await service.start_simulation_runs([run_id], SAMPLE_REQUEST)

    assert outcome is DispatchOutcome.NOT_RECORDED
    assert service.dispatcher.cancelled == ["fc-1"]
    assert "simulation_run_call_not_recorded" in caplog.text


@pytest.mark.asyncio
async def test_a_call_that_cannot_be_recorded_is_cancelled_and_its_runs_failed(
    caplog,
) -> None:
    service, transitions, _ = _service()
    run_id = transitions.add_run()

    def unreachable_database(*_arguments, **_keywords):
        msg = "connection lost"
        raise ConnectionError(msg)

    transitions.record_simulation_dispatched = unreachable_database  # type: ignore[method-assign]

    outcome = await service.start_simulation_runs([run_id], SAMPLE_REQUEST)

    assert outcome is DispatchOutcome.NOT_RECORDED
    assert service.dispatcher.cancelled == ["fc-1"]
    run = execution(transitions, run_id)
    assert run.status is RunStatus.FAILED
    assert run.error.code is RunErrorCode.DISPATCH_FAILED
    assert run.error.message == "The Modal call could not be recorded."
    assert any(
        getattr(record, "modal_call_id", None) == "fc-1" for record in caplog.records
    )


@pytest.mark.asyncio
async def test_a_failed_cancel_is_logged_without_raising(caplog) -> None:
    service, transitions, _ = _service(
        FakeDispatcher(cancel_failure=RuntimeError("modal down"))
    )
    run_id = transitions.add_run()

    def unreachable_database(*_arguments, **_keywords):
        msg = "connection lost"
        raise ConnectionError(msg)

    transitions.record_simulation_dispatched = unreachable_database  # type: ignore[method-assign]

    outcome = await service.start_simulation_runs([run_id], SAMPLE_REQUEST)

    assert outcome is DispatchOutcome.NOT_RECORDED
    assert "simulation_run_call_not_cancelled" in caplog.text


def _commit_then_lose_acknowledgement(record):
    """Return ``record`` changed to commit and then raise, as a lost reply does."""

    def committed_without_acknowledgement(*arguments, **keywords):
        assert record(*arguments, **keywords)
        msg = "connection lost after the commit"
        raise ConnectionError(msg)

    return committed_without_acknowledgement


@pytest.mark.asyncio
async def test_an_unacknowledged_record_keeps_its_call() -> None:
    service, transitions, _ = _service()
    run_id = transitions.add_run()
    transitions.record_simulation_dispatched = _commit_then_lose_acknowledgement(  # type: ignore[method-assign]
        transitions.record_simulation_dispatched
    )

    outcome = await service.start_simulation_runs([run_id], SAMPLE_REQUEST)

    assert outcome is DispatchOutcome.STARTED
    assert service.dispatcher.cancelled == []
    run = execution(transitions, run_id)
    assert run.status is RunStatus.RUNNING
    assert run.modal_call_id == "fc-1"


@pytest.mark.asyncio
async def test_an_unacknowledged_comparison_record_keeps_its_call() -> None:
    service, transitions, _ = _service()
    comparison = transitions.add_comparison()
    transitions.record_comparison_dispatched = _commit_then_lose_acknowledgement(  # type: ignore[method-assign]
        transitions.record_comparison_dispatched
    )

    outcome = await service.start_comparison(comparison, SAMPLE_REQUEST)

    assert outcome is DispatchOutcome.STARTED
    assert service.dispatcher.cancelled == []
    assert transitions.comparisons[comparison].status is RunStatus.RUNNING


@pytest.mark.asyncio
async def test_a_failed_record_of_runs_failed_meanwhile_cancels_the_call() -> None:
    service, transitions, _ = _service()
    run_id = transitions.add_run()

    def abandoned_meanwhile(*_arguments, **_keywords):
        transitions.runs[run_id].status = RunStatus.FAILED
        msg = "connection lost"
        raise ConnectionError(msg)

    transitions.record_simulation_dispatched = abandoned_meanwhile  # type: ignore[method-assign]

    outcome = await service.start_simulation_runs([run_id], SAMPLE_REQUEST)

    assert outcome is DispatchOutcome.NOT_RECORDED
    assert service.dispatcher.cancelled == ["fc-1"]


@pytest.mark.asyncio
async def test_a_call_whose_record_cannot_be_read_is_left_running(caplog) -> None:
    """With the database unreachable, the record may have committed all the same."""
    service, transitions, _ = _service()
    run_id = transitions.add_run()

    def unreachable_database(*_arguments, **_keywords):
        msg = "connection lost"
        raise ConnectionError(msg)

    transitions.record_simulation_dispatched = unreachable_database  # type: ignore[method-assign]
    transitions.record_simulation_dispatch_failure = unreachable_database  # type: ignore[method-assign]

    outcome = await service.start_simulation_runs([run_id], SAMPLE_REQUEST)

    assert outcome is DispatchOutcome.RECORD_UNKNOWN
    assert service.dispatcher.cancelled == []
    assert any(
        record.message == "simulation_run_call_record_unknown"
        and getattr(record, "modal_call_id", None) == "fc-1"
        for record in caplog.records
    )


def test_the_dispatcher_cancels_a_call_by_its_id(monkeypatch) -> None:
    cancelled = []

    class _FakeCancel:
        async def aio(self):
            cancelled.append("cancelled")

    class _FakeCall:
        cancel = _FakeCancel()

    monkeypatch.setattr(
        modal_calls.modal.FunctionCall, "from_id", lambda call_id: _FakeCall()
    )

    asyncio.run(ModalCallDispatcher("staging").cancel("fc-orphan"))

    assert cancelled == ["cancelled"]


@pytest.mark.asyncio
async def test_a_comparison_starts_only_once_its_runs_succeeded() -> None:
    service, transitions, _ = _service()
    waiting = transitions.add_comparison(runs_succeeded=False)
    ready = transitions.add_comparison()

    assert (
        await service.start_comparison(waiting, SAMPLE_REQUEST)
        is DispatchOutcome.NOT_CLAIMED
    )
    assert (
        await service.start_comparison(ready, SAMPLE_REQUEST) is DispatchOutcome.STARTED
    )
    assert transitions.comparisons[ready].modal_call_id == "fc-1"
    assert len(service.dispatcher.requests) == 1


@pytest.mark.asyncio
async def test_a_refused_comparison_call_fails_the_step(caplog) -> None:
    service, transitions, _ = _service(FakeDispatcher(failure=RuntimeError("down")))
    comparison = transitions.add_comparison()

    outcome = await service.start_comparison(comparison, SAMPLE_REQUEST)

    assert outcome is DispatchOutcome.DISPATCH_FAILED
    assert transitions.comparisons[comparison].status is RunStatus.FAILED
    assert [record.message for record in caplog.records] == [
        "report_run_comparison_dispatch_failed"
    ]


class _FakeCall:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.get = self

    async def aio(self, *, timeout):
        assert timeout == 0
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        return self.behaviour


def _inspect(monkeypatch, behaviour):
    monkeypatch.setattr(
        modal_calls.modal.FunctionCall,
        "from_id",
        lambda call_id: _FakeCall(behaviour),
    )
    return asyncio.run(ModalCallInspector().inspect("fc-test"))


def test_a_call_without_output_is_running(monkeypatch) -> None:
    assert _inspect(monkeypatch, TimeoutError()) == CallRunning()


def test_a_finished_call_returns_its_output(monkeypatch) -> None:
    assert _inspect(monkeypatch, {"budget": 1.0}) == CallSucceeded({"budget": 1.0})


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (modal.exception.OutputExpiredError(), RunErrorCode.RESULT_EXPIRED),
        (modal.exception.NotFoundError("gone"), RunErrorCode.CALL_NOT_FOUND),
    ],
    ids=["expired", "not-found"],
)
def test_modal_answers_about_the_call_itself_fail_the_run(
    monkeypatch, raised, code
) -> None:
    outcome = _inspect(monkeypatch, raised)

    assert isinstance(outcome, CallFailed)
    assert outcome.error.code is code


def test_a_failed_call_records_only_the_failure_type(monkeypatch) -> None:
    outcome = _inspect_finished(monkeypatch, KeyError("household secret"))

    assert outcome == CallFailed(
        RunError(RunErrorCode.WORKER_ERROR, "The Modal call raised KeyError.")
    )


@pytest.mark.parametrize(
    "raised",
    [
        modal.exception.ConnectionError("unreachable"),
        modal.exception.ServiceError("unavailable"),
        modal.exception.PermissionDeniedError("token revoked"),
        modal.exception.VersionError("client too old"),
        modal.exception.ClientClosed("closed"),
        grpclib.exceptions.StreamTerminatedError("stream reset"),
        grpclib.exceptions.GRPCError(grpclib.const.Status.UNAVAILABLE, "down"),
        RuntimeError("an unexpected client problem"),
    ],
    ids=[
        "connection",
        "service",
        "permission",
        "version",
        "client-closed",
        "stream-terminated",
        "grpc",
        "other-client-error",
    ],
)
def test_a_modal_problem_is_retried_later(monkeypatch, raised) -> None:
    with pytest.raises(CallInspectionError):
        _inspect(monkeypatch, raised)


class _FinishedCall:
    """A call whose output Modal holds: the worker's exception, or a timeout.

    Reading it goes through Modal's own result processing, which raises the
    worker's exception in this process, as a real finished call does.
    """

    def __init__(self, worker_exception, status=None):
        self.result = api_pb2.GenericResult(
            status=status or api_pb2.GenericResult.GENERIC_STATUS_FAILURE,
            data=serialize(worker_exception),
            exception=repr(worker_exception),
        )
        self.get = self

    async def aio(self, *, timeout):
        assert timeout == 0
        return await _process_result(
            self.result, api_pb2.DATA_FORMAT_PICKLE, None, None
        )


def _inspect_finished(monkeypatch, worker_exception, status=None):
    monkeypatch.setattr(
        modal_calls.modal.FunctionCall,
        "from_id",
        lambda call_id: _FinishedCall(worker_exception, status),
    )
    return asyncio.run(ModalCallInspector().inspect("fc-test"))


@pytest.mark.parametrize(
    "worker_exception",
    [
        SystemExit(2),
        builtins.BaseExceptionGroup("worker", [ValueError("one")]),
        KeyboardInterrupt(),
        TimeoutError("a child call timed out"),
        modal.exception.ConnectionError("the worker's own Modal call failed"),
        grpclib.exceptions.GRPCError(grpclib.const.Status.UNAVAILABLE, "down"),
    ],
    ids=[
        "system-exit",
        "exception-group",
        "keyboard-interrupt",
        "timeout",
        "modal-connection",
        "grpc",
    ],
)
def test_whatever_the_worker_raised_fails_the_run(
    monkeypatch, worker_exception
) -> None:
    """The worker's own exception, even of a type the client also raises, is final."""
    outcome = _inspect_finished(monkeypatch, worker_exception)

    assert isinstance(outcome, CallFailed)
    assert outcome.error.code is RunErrorCode.WORKER_ERROR
    assert type(worker_exception).__name__ in outcome.error.message


def test_a_call_modal_failed_internally_fails_the_run(monkeypatch) -> None:
    outcome = _inspect_finished(
        monkeypatch,
        RuntimeError("ignored"),
        status=api_pb2.GenericResult.GENERIC_STATUS_INTERNAL_FAILURE,
    )

    assert isinstance(outcome, CallFailed)
    assert outcome.error.code is RunErrorCode.WORKER_ERROR


def test_a_call_modal_timed_out_fails_as_a_timeout(monkeypatch) -> None:
    outcome = _inspect_finished(
        monkeypatch,
        RuntimeError("ignored"),
        status=api_pb2.GenericResult.GENERIC_STATUS_TIMEOUT,
    )

    assert outcome == CallFailed(
        RunError(RunErrorCode.WORKER_TIMEOUT, "The Modal call timed out.")
    )


BLOB_DOWNLOAD_URL = "https://blob.test/output"


class _BlobStub:
    """Modal's API as its result processing uses it to fetch a stored output."""

    def __init__(self, failure=None):
        self.failure = failure

    async def BlobGet(self, request):  # noqa: N802  # Modal's method name
        assert request.blob_id == "bl-test"
        if self.failure is not None:
            raise self.failure
        return api_pb2.BlobGetResponse(download_url=BLOB_DOWNLOAD_URL)


class _BlobStoredCall:
    """A finished call whose output Modal stores as a blob.

    Modal does so for any spawned call's output over 8 KiB, so nearly every
    real result is read this way: Modal's result processing first makes this
    process's own requests for the blob, then decodes it.
    """

    def __init__(self, stub):
        self.result = api_pb2.GenericResult(
            status=api_pb2.GenericResult.GENERIC_STATUS_SUCCESS,
            data_blob_id="bl-test",
        )
        self.stub = stub
        self.get = self

    async def aio(self, *, timeout):
        assert timeout == 0
        return await _process_result(
            self.result, api_pb2.DATA_FORMAT_PICKLE, self.stub, None
        )


def _inspect_blob_stored(monkeypatch, stub, download):
    async def fake_download(download_url):
        assert download_url == BLOB_DOWNLOAD_URL
        if isinstance(download, BaseException):
            raise download
        return download

    monkeypatch.setattr(blob_utils, "_download_from_url", fake_download)
    monkeypatch.setattr(
        modal_calls.modal.FunctionCall,
        "from_id",
        lambda call_id: _BlobStoredCall(stub),
    )
    return asyncio.run(ModalCallInspector().inspect("fc-test"))


def test_an_output_stored_as_a_blob_is_read(monkeypatch) -> None:
    outcome = _inspect_blob_stored(monkeypatch, _BlobStub(), serialize({"budget": 1.0}))

    assert outcome == CallSucceeded({"budget": 1.0})


@pytest.mark.parametrize(
    ("blob_request_failure", "download_failure"),
    [
        (modal.exception.ExecutionError("blob request failed"), None),
        (grpclib.exceptions.GRPCError(grpclib.const.Status.UNAVAILABLE, "down"), None),
        (modal.exception.ConnectionError("unreachable"), None),
        (None, modal.exception.ExecutionError("Get from url failed with status 503")),
    ],
    ids=["blob-request-execution-error", "blob-request-grpc", "connection", "download"],
)
def test_a_failed_output_download_is_retried_later(
    monkeypatch, blob_request_failure, download_failure
) -> None:
    """Requests for a stored output are the entrypoint's own, not the worker's."""
    with pytest.raises(CallInspectionError):
        _inspect_blob_stored(
            monkeypatch, _BlobStub(blob_request_failure), download_failure
        )


def test_an_output_download_that_times_out_is_checked_again(monkeypatch) -> None:
    outcome = _inspect_blob_stored(monkeypatch, _BlobStub(), TimeoutError())

    assert outcome == CallRunning()


def _worker_exception_from_its_own_download():
    """Return what a worker raises when its own Modal blob download fails."""

    async def worker():
        await blob_utils.blob_download(
            "bl-test", _BlobStub(modal.exception.ConnectionError("worker's download"))
        )

    try:
        asyncio.run(worker())
    except modal.exception.ConnectionError as error:
        return error
    raise AssertionError


def test_a_worker_exception_with_its_traceback_fails_the_run(monkeypatch) -> None:
    """The worker's frames follow Modal's result processing, whatever they hold."""
    worker_exception = _worker_exception_from_its_own_download()
    serialized_tb, tb_line_cache = pickle_traceback(worker_exception, "ta-test")
    call = _FinishedCall(worker_exception)
    call.result.serialized_tb = serialized_tb
    call.result.tb_line_cache = tb_line_cache
    monkeypatch.setattr(modal_calls.modal.FunctionCall, "from_id", lambda call_id: call)

    outcome = asyncio.run(ModalCallInspector().inspect("fc-test"))

    assert outcome == CallFailed(
        RunError(RunErrorCode.WORKER_ERROR, "The Modal call raised ConnectionError.")
    )


@pytest.mark.parametrize(
    "signal",
    [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit(0)],
    ids=["cancelled", "keyboard-interrupt", "system-exit"],
)
def test_a_signal_to_this_process_is_not_swallowed(monkeypatch, signal) -> None:
    """Only an exception that came from the call's result is the worker's."""
    with pytest.raises(type(signal)):
        _inspect(monkeypatch, signal)


def test_the_dispatcher_passes_observability_as_its_own_argument(monkeypatch) -> None:
    spawned = {}

    class _FakeSpawn:
        async def aio(self, *arguments, observability_context):
            spawned.update(arguments=arguments, context=observability_context)
            return type("Call", (), {"object_id": "fc-spawned"})()

    class _FakeFunction:
        spawn = _FakeSpawn()

    def from_name(application, function, *, environment_name):
        spawned.update(target=(application, function, environment_name))
        return _FakeFunction()

    monkeypatch.setattr(modal_calls.modal.Function, "from_name", from_name)

    call_id = asyncio.run(ModalCallDispatcher("staging").spawn(SAMPLE_REQUEST))

    assert call_id == "fc-spawned"
    assert spawned == {
        "target": ("policyengine-simulation-v2", "run", "staging"),
        "arguments": ({"country": "us"},),
        "context": {"traceparent": "00-test"},
    }
