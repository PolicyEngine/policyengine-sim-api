"""The background loop that records Modal call outcomes on runs."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from conftest import FakeBackend, make_settings
from fastapi.testclient import TestClient
from policyengine_simulation_entry import run_recording
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.modal_calls import (
    CallFailed,
    CallInspectionError,
    CallSucceeded,
)
from policyengine_simulation_entry.run_recording import (
    BUSY_PASS_INTERVAL_SECONDS,
    CHECK_LEASE,
    IDLE_PASS_INTERVAL_SECONDS,
    RecordingPass,
    ResultRecorders,
    RunRecorder,
    RunRecordingLoop,
    next_check_delay,
)
from policyengine_simulation_persistence import (
    HEAD_REVISION,
    ReportKind,
    RunError,
    RunErrorCode,
    RunStatus,
    SimulationKind,
)
from policyengine_simulation_persistence.engine import MAX_OVERFLOW, POOL_SIZE
from pydantic import BaseModel
from run_fakes import FakeInspector, FakeRunTransitions, ManualClock


class BudgetResult(BaseModel):
    budgetary_impact: float


class RecordingRecorder:
    """Validate outputs as ``BudgetResult`` and remember what it wrote."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.writes = []

    def validate(self, output):
        return BudgetResult.model_validate(output)

    def write(self, connection, records, result) -> None:
        if self.failure is not None:
            raise self.failure
        self.writes.append((connection, records, result))


def _recorder(recorders=None):
    clock = ManualClock()
    transitions = FakeRunTransitions(clock)
    inspector = FakeInspector()
    recorder = RunRecorder(
        transitions=transitions,
        inspector=inspector,
        recorders=recorders or ResultRecorders(),
        clock=clock,
    )
    return recorder, transitions, inspector, clock


def _run(recorder) -> RecordingPass:
    return asyncio.run(recorder.record_due_outcomes())


@pytest.mark.parametrize(
    ("check_count", "seconds"),
    [(0, 2), (1, 2), (2, 4), (3, 8), (5, 32), (6, 60), (40, 60)],
)
def test_checks_back_off_up_to_a_minute(check_count, seconds) -> None:
    assert next_check_delay(check_count) == timedelta(seconds=seconds)


def test_a_running_call_is_checked_again_later() -> None:
    recorder, transitions, inspector, clock = _recorder()
    (run_id,) = transitions.running_call("fc-1")

    recording = _run(recorder)

    assert inspector.inspected == ["fc-1"]
    assert recording.still_running == 1
    run = transitions.runs[run_id]
    assert run.status is RunStatus.RUNNING
    assert run.check_count == 1
    assert run.next_check_at == clock() + timedelta(seconds=2)
    assert _run(recorder).checked == 0


def test_a_failed_call_fails_its_runs() -> None:
    recorder, transitions, inspector, _ = _recorder()
    (run_id,) = transitions.running_call("fc-1")
    inspector.outcomes["fc-1"] = CallFailed(
        RunError(RunErrorCode.WORKER_TIMEOUT, "The Modal call timed out.")
    )

    recording = _run(recorder)

    assert recording.failed == 1
    run = transitions.runs[run_id]
    assert run.status is RunStatus.FAILED
    assert run.error.code is RunErrorCode.WORKER_TIMEOUT


def test_a_finished_call_is_validated_and_written_with_its_success() -> None:
    budget = RecordingRecorder()
    recorder, transitions, inspector, _ = _recorder(
        ResultRecorders(simulations={SimulationKind.ECONOMY: budget})
    )
    (run_id,) = transitions.running_call("fc-1")
    inspector.outcomes["fc-1"] = CallSucceeded({"budgetary_impact": -1.5})

    recording = _run(recorder)

    assert recording.succeeded == 1
    run = transitions.runs[run_id]
    assert run.status is RunStatus.SUCCEEDED
    ((connection, records, result),) = budget.writes
    assert [record.id for record in records] == [run_id]
    assert result == BudgetResult(budgetary_impact=-1.5)
    assert run.written_results == [connection]


def test_a_household_pair_is_written_once_for_both_runs() -> None:
    household = RecordingRecorder()
    recorder, transitions, inspector, _ = _recorder(
        ResultRecorders(simulations={SimulationKind.HOUSEHOLD: household})
    )
    pair = transitions.running_call("fc-pair", "household", "household")
    inspector.outcomes["fc-pair"] = CallSucceeded({"budgetary_impact": 0})

    recording = _run(recorder)

    assert recording.checked == 1
    assert inspector.inspected == ["fc-pair"]
    ((_, records, _),) = household.writes
    assert {record.id for record in records} == set(pair)
    assert {transitions.runs[run_id].status for run_id in pair} == {RunStatus.SUCCEEDED}


def test_a_kind_without_a_recorder_waits_for_an_instance_with_one() -> None:
    """An older revision still running beside a new one must not fail the run."""
    recorder, transitions, inspector, clock = _recorder()
    (run_id,) = transitions.running_call("fc-1")
    inspector.outcomes["fc-1"] = CallSucceeded({"budgetary_impact": 1})

    recording = _run(recorder)

    assert recording.without_recorder == 1
    run = transitions.runs[run_id]
    assert run.status is RunStatus.RUNNING
    assert run.error is None
    assert run.next_check_at == clock() + timedelta(seconds=2)


def test_an_invalid_output_fails_its_runs_without_repeating_it() -> None:
    recorder, transitions, inspector, _ = _recorder(
        ResultRecorders(simulations={SimulationKind.ECONOMY: RecordingRecorder()})
    )
    (run_id,) = transitions.running_call("fc-1")
    inspector.outcomes["fc-1"] = CallSucceeded({"budgetary_impact": "household 42"})

    recording = _run(recorder)

    assert recording.failed == 1
    error = transitions.runs[run_id].error
    assert error.code is RunErrorCode.INVALID_RESULT
    assert "1 errors" in error.message
    assert "household 42" not in error.message


def test_an_unreachable_modal_leaves_the_call_for_after_its_lease() -> None:
    recorder, transitions, inspector, clock = _recorder()
    (run_id,) = transitions.running_call("fc-1")
    inspector.outcomes["fc-1"] = CallInspectionError("ConnectionError")

    recording = _run(recorder)

    assert recording.not_reached == 1
    run = transitions.runs[run_id]
    assert run.status is RunStatus.RUNNING
    assert run.next_check_at == clock() + CHECK_LEASE
    clock.advance(CHECK_LEASE)
    assert _run(recorder).checked == 1


def test_a_failed_result_write_records_nothing_and_is_retried() -> None:
    recorder, transitions, inspector, clock = _recorder(
        ResultRecorders(
            simulations={
                SimulationKind.ECONOMY: RecordingRecorder(
                    failure=ConnectionError("database unreachable")
                )
            }
        )
    )
    (run_id,) = transitions.running_call("fc-1")
    inspector.outcomes["fc-1"] = CallSucceeded({"budgetary_impact": 1})

    recording = _run(recorder)

    assert recording.not_reached == 1
    assert recording.succeeded == 0
    run = transitions.runs[run_id]
    assert run.status is RunStatus.RUNNING
    assert run.next_check_at == clock() + CHECK_LEASE


def test_an_outcome_another_instance_recorded_first_is_not_counted() -> None:
    recorder, transitions, _, _ = _recorder(
        ResultRecorders(simulations={SimulationKind.ECONOMY: RecordingRecorder()})
    )
    (run_id,) = transitions.running_call("fc-1")

    class RacingInspector(FakeInspector):
        async def inspect(self, modal_call_id):
            transitions.runs[run_id].status = RunStatus.SUCCEEDED
            return CallSucceeded({"budgetary_impact": 1})

    recorder.inspector = RacingInspector()

    recording = _run(recorder)

    assert recording.checked == 1
    assert recording.succeeded == 0


def test_comparison_steps_are_checked_and_recorded() -> None:
    comparisons = RecordingRecorder()
    recorder, transitions, inspector, clock = _recorder(
        ResultRecorders(comparisons={ReportKind.ECONOMY: comparisons})
    )
    finished = transitions.add_comparison(
        status=RunStatus.RUNNING, modal_call_id="fc-done", next_check_at=clock()
    )
    waiting = transitions.add_comparison(
        status=RunStatus.RUNNING, modal_call_id="fc-wait", next_check_at=clock()
    )
    inspector.outcomes["fc-done"] = CallSucceeded({"budgetary_impact": 3})

    recording = _run(recorder)

    assert recording.succeeded == 1
    assert recording.still_running == 1
    assert transitions.comparisons[finished].status is RunStatus.SUCCEEDED
    assert [record.id for _, record, _ in comparisons.writes] == [finished]
    assert transitions.comparisons[waiting].next_check_at == clock() + timedelta(
        seconds=2
    )


def test_dispatches_left_for_ten_minutes_are_failed_by_a_pass() -> None:
    recorder, transitions, _, clock = _recorder()
    stalled = transitions.add_run(status=RunStatus.DISPATCHING, updated_at=clock())
    clock.advance(timedelta(minutes=10, seconds=1))
    recent = transitions.add_run(status=RunStatus.DISPATCHING, updated_at=clock())

    recording = _run(recorder)

    assert recording.abandoned == 1
    assert transitions.runs[stalled].status is RunStatus.FAILED
    assert transitions.runs[recent].status is RunStatus.DISPATCHING


def test_a_pass_fails_comparisons_whose_simulation_run_failed() -> None:
    recorder, transitions, _, _ = _recorder()
    stuck = transitions.add_comparison(runs_failed=True)
    waiting = transitions.add_comparison(runs_succeeded=False)

    recording = _run(recorder)

    assert recording.comparisons_without_runs == 1
    assert recording.found_work
    assert transitions.comparisons[stuck].status is RunStatus.FAILED
    assert transitions.comparisons[stuck].error.code is (
        RunErrorCode.SIMULATION_RUN_FAILED
    )
    assert transitions.comparisons[waiting].status is RunStatus.PENDING


def test_comparison_outcomes_are_logged_as_comparison_events(caplog) -> None:
    recorder, transitions, inspector, clock = _recorder()
    transitions.add_comparison(
        status=RunStatus.RUNNING, modal_call_id="fc-failed", next_check_at=clock()
    )
    inspector.outcomes["fc-failed"] = CallFailed(
        RunError(RunErrorCode.WORKER_ERROR, "The Modal call raised KeyError.")
    )

    _run(recorder)

    assert [record.message for record in caplog.records] == [
        "report_run_comparison_failed"
    ]


def test_the_loop_leaves_database_connections_for_requests() -> None:
    assert run_recording.INSPECTION_CONCURRENCY < POOL_SIZE + MAX_OVERFLOW


class _ScriptedRecorder:
    """Return scripted passes, or raise, and count how often it ran."""

    def __init__(self, passes) -> None:
        self.passes = list(passes)
        self.calls = 0

    async def record_due_outcomes(self) -> RecordingPass:
        self.calls += 1
        outcome = self.passes.pop(0) if self.passes else RecordingPass()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_the_loop_waits_less_while_there_is_work_and_survives_failures() -> None:
    scripted = _ScriptedRecorder(
        [RecordingPass(checked=3), RecordingPass(), RuntimeError("database down")]
    )
    waits = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 5:
            raise asyncio.CancelledError

    loop = RunRecordingLoop(scripted, sleep=sleep)  # type: ignore[arg-type]

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(loop.run())

    assert waits == [
        IDLE_PASS_INTERVAL_SECONDS,
        BUSY_PASS_INTERVAL_SECONDS,
        IDLE_PASS_INTERVAL_SECONDS,
        IDLE_PASS_INTERVAL_SECONDS,
        IDLE_PASS_INTERVAL_SECONDS,
    ]
    assert scripted.calls == 4


def test_the_loop_starts_and_stops_as_a_background_task() -> None:
    scripted = _ScriptedRecorder([])

    async def lifetime() -> None:
        loop = RunRecordingLoop(scripted, sleep=lambda _seconds: asyncio.sleep(0))  # type: ignore[arg-type]
        loop.start()
        await asyncio.sleep(0.01)
        await loop.stop()
        await loop.stop()

    asyncio.run(lifetime())

    assert scripted.calls > 0


class _TrackedLoop:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _DatabaseWithLoop:
    def __init__(self) -> None:
        self.loop = _TrackedLoop()

    async def read_revision(self):
        return HEAD_REVISION

    def recording_loop(self):
        return self.loop

    def dispose(self) -> None:
        return None


def test_each_instance_runs_the_loop_for_its_lifetime() -> None:
    database = _DatabaseWithLoop()
    app = create_app(
        settings=make_settings(
            simulation_database_url="postgresql://runtime:secret@db.example/postgres"
        ),
        backend=FakeBackend(),
        auth_dependency=lambda: None,
        simulation_database=database,
    )

    with TestClient(app):
        assert database.loop.started is True
        assert database.loop.stopped is False

    assert database.loop.stopped is True
