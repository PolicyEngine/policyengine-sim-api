"""Starting a run's Modal call exactly once, and reading Modal's answers."""

from __future__ import annotations

import asyncio

import modal
import pytest
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
from policyengine_simulation_persistence import RunErrorCode, RunStatus
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
async def test_a_call_whose_runs_stopped_dispatching_is_reported() -> None:
    service, transitions, _ = _service()
    run_id = transitions.add_run()

    class SlowDispatcher(FakeDispatcher):
        async def spawn(self, request):
            # The recording loop gave up on the dispatch in the meantime.
            transitions.runs[run_id].status = RunStatus.FAILED
            return await super().spawn(request)

    service.dispatcher = SlowDispatcher()

    outcome = await service.start_simulation_runs([run_id], SAMPLE_REQUEST)

    assert outcome is DispatchOutcome.NOT_RECORDED
    assert execution(transitions, run_id).modal_call_id is None


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
async def test_a_refused_comparison_call_fails_the_step() -> None:
    service, transitions, _ = _service(FakeDispatcher(failure=RuntimeError("down")))
    comparison = transitions.add_comparison()

    outcome = await service.start_comparison(comparison, SAMPLE_REQUEST)

    assert outcome is DispatchOutcome.DISPATCH_FAILED
    assert transitions.comparisons[comparison].status is RunStatus.FAILED


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
        (modal.exception.FunctionTimeoutError("slow"), RunErrorCode.WORKER_TIMEOUT),
        (modal.exception.OutputExpiredError(), RunErrorCode.RESULT_EXPIRED),
        (modal.exception.NotFoundError("gone"), RunErrorCode.CALL_NOT_FOUND),
        (KeyError("household secret"), RunErrorCode.WORKER_ERROR),
        (modal.exception.InternalFailure("lost"), RunErrorCode.WORKER_ERROR),
    ],
    ids=["timeout", "expired", "not-found", "worker-error", "internal-failure"],
)
def test_a_failed_call_records_only_the_failure_type(monkeypatch, raised, code) -> None:
    outcome = _inspect(monkeypatch, raised)

    assert isinstance(outcome, CallFailed)
    assert outcome.error.code is code
    assert "secret" not in outcome.error.message


@pytest.mark.parametrize(
    "raised",
    [
        modal.exception.ConnectionError("unreachable"),
        modal.exception.ServiceError("unavailable"),
    ],
    ids=["connection", "service"],
)
def test_a_modal_problem_is_retried_later(monkeypatch, raised) -> None:
    with pytest.raises(CallInspectionError):
        _inspect(monkeypatch, raised)


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
