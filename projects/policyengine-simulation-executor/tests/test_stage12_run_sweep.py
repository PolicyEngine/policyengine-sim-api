"""The sweep that fails runs left running after their Modal call ended."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum
from types import SimpleNamespace
from uuid import uuid4

import pytest
from policyengine_simulation_executor.stage12_runtime.run_sweep import (
    CallState,
    SweepSummary,
    modal_call_state,
    sweep_stuck_runs,
)
from policyengine_stage12_persistence import RunKind, RunningRun, RunStatus, RunWrite

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


class FakeInputStatus(Enum):
    """Stands in for ``modal.call_graph.InputStatus``; the sweep reads names."""

    PENDING = 0
    SUCCESS = 1
    FAILURE = 2
    TERMINATED = 3
    TIMEOUT = 4
    INIT_FAILURE = 5


class FakeSweepStore:
    def __init__(self, runs):
        self.runs = list(runs)
        self.cutoffs = []
        self.failed = []
        self.events = []

    def list_running_runs(self, kind, *, started_before):
        self.cutoffs.append(started_before)
        self.events.append(f"list:{kind.value}")
        return tuple(run for run in self.runs if run.kind is kind)

    def fail_run_of_ended_call(self, run, *, error_message, failed_at):
        self.failed.append((run, error_message, failed_at))
        self.events.append(f"fail:{run.kind.value}")
        return RunWrite(changed=True, status=RunStatus.FAILED)


def _run(call_id, kind=RunKind.SIMULATION_RUN):
    return RunningRun(kind=kind, run_id=uuid4(), modal_call_id=call_id)


def test_only_runs_whose_call_ended_are_failed() -> None:
    ended = _run("fc-ended")
    alive = _run("fc-alive")
    unknown = _run("fc-unknown")
    store = FakeSweepStore([ended, alive, unknown])
    states = {
        "fc-ended": CallState.ENDED,
        "fc-alive": CallState.RUNNING,
        "fc-unknown": CallState.UNKNOWN,
    }

    summary = sweep_stuck_runs(store, call_state=states.__getitem__, now=NOW)

    assert [run for run, _, _ in store.failed] == [ended]
    (_, message, failed_at) = store.failed[0]
    assert message.startswith("modal_call_ended: ")
    assert failed_at == NOW
    assert summary == SweepSummary(checked=3, failed=1, running=1, unknown=1)


def test_the_sweep_lists_runs_older_than_its_minimum_age() -> None:
    store = FakeSweepStore([])

    sweep_stuck_runs(
        store,
        call_state=lambda _: CallState.ENDED,
        now=NOW,
        minimum_age=timedelta(minutes=15),
    )

    assert store.cutoffs == [NOW - timedelta(minutes=15)] * 2


def test_report_runs_are_swept_before_simulation_runs_are_listed() -> None:
    # A simulation run is listed only once no report run combining it is
    # running, so failing a dead coordinator's report run first lets the same
    # sweep reach its children.
    report_run = _run("fc-coordinator", RunKind.REPORT_RUN)
    simulation_run = _run("fc-worker")
    store = FakeSweepStore([simulation_run, report_run])

    sweep_stuck_runs(store, call_state=lambda _: CallState.ENDED, now=NOW)

    assert store.events == [
        "list:report_run",
        "fail:report_run",
        "list:simulation_run",
        "fail:simulation_run",
    ]


def test_a_call_running_several_runs_is_asked_once() -> None:
    report_run = _run("fc-coordinator", RunKind.REPORT_RUN)
    other = _run("fc-coordinator", RunKind.REPORT_RUN)
    asked = []

    def call_state(call_id):
        asked.append(call_id)
        return CallState.ENDED

    sweep_stuck_runs(
        FakeSweepStore([report_run, other]), call_state=call_state, now=NOW
    )

    assert asked == ["fc-coordinator"]


def test_a_failed_status_lookup_never_fails_the_run() -> None:
    run = _run("fc-error")
    store = FakeSweepStore([run])

    def call_state(_call_id):
        msg = "Modal is unreachable"
        raise ConnectionError(msg)

    summary = sweep_stuck_runs(store, call_state=call_state, now=NOW)

    assert store.failed == []
    assert summary == SweepSummary(checked=1, failed=0, running=0, unknown=1)


def test_a_run_another_sweep_failed_first_is_not_counted_again() -> None:
    run = _run("fc-ended")

    class AlreadyFailedStore(FakeSweepStore):
        def fail_run_of_ended_call(self, run, *, error_message, failed_at):
            super().fail_run_of_ended_call(
                run, error_message=error_message, failed_at=failed_at
            )
            return RunWrite(changed=False, status=RunStatus.FAILED)

    summary = sweep_stuck_runs(
        AlreadyFailedStore([run]), call_state=lambda _: CallState.ENDED, now=NOW
    )

    assert summary.failed == 0


def _node(call_id, status, children=()):
    return SimpleNamespace(
        function_call_id=call_id, status=status, children=list(children)
    )


def _fake_modal(statuses=None, error=None, *, graph=None):
    """Return a modal stand-in whose call graph holds call fc-1's inputs.

    By default fc-1 is a top-level call; ``graph`` gives the whole graph a
    spawned call is part of instead.
    """

    def from_id(call_id):
        if error is not None:
            raise error
        assert call_id == "fc-1"
        roots = (
            graph
            if graph is not None
            else [_node("fc-1", status) for status in (statuses or [])]
        )
        return SimpleNamespace(get_call_graph=lambda: roots)

    return SimpleNamespace(FunctionCall=SimpleNamespace(from_id=from_id))


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([FakeInputStatus.PENDING], CallState.RUNNING),
        ([FakeInputStatus.SUCCESS], CallState.ENDED),
        ([FakeInputStatus.FAILURE], CallState.ENDED),
        ([FakeInputStatus.TERMINATED], CallState.ENDED),
        ([FakeInputStatus.TIMEOUT], CallState.ENDED),
        ([FakeInputStatus.INIT_FAILURE], CallState.ENDED),
        ([FakeInputStatus.FAILURE, FakeInputStatus.PENDING], CallState.RUNNING),
        ([], CallState.UNKNOWN),
    ],
)
def test_modal_call_state_reads_the_call_graph(statuses, expected) -> None:
    assert modal_call_state("fc-1", modal_module=_fake_modal(statuses)) is expected


def test_modal_call_state_is_unknown_when_modal_cannot_answer() -> None:
    state = modal_call_state(
        "fc-1", modal_module=_fake_modal(error=ConnectionError("unreachable"))
    )

    assert state is CallState.UNKNOWN


@pytest.mark.parametrize(
    ("coordinator_status", "worker_status", "expected"),
    [
        # A coordinator that ran out of memory left its worker running.
        (FakeInputStatus.FAILURE, FakeInputStatus.PENDING, CallState.RUNNING),
        (FakeInputStatus.PENDING, FakeInputStatus.TERMINATED, CallState.ENDED),
    ],
)
def test_modal_call_state_reads_the_recorded_calls_own_inputs(
    coordinator_status, worker_status, expected
) -> None:
    # A worker is spawned inside its coordinator's input, so Modal nests its
    # inputs under the coordinator's in the call graph.
    graph = [
        _node(
            "fc-coordinator",
            coordinator_status,
            children=[
                _node("fc-1", worker_status, children=[_node("fc-segment", "x")]),
                _node("fc-other-worker", FakeInputStatus.SUCCESS),
            ],
        )
    ]

    assert modal_call_state("fc-1", modal_module=_fake_modal(graph=graph)) is expected


def test_a_call_missing_from_its_call_graph_is_unknown() -> None:
    graph = [_node("fc-coordinator", FakeInputStatus.SUCCESS)]

    assert (
        modal_call_state("fc-1", modal_module=_fake_modal(graph=graph))
        is CallState.UNKNOWN
    )
