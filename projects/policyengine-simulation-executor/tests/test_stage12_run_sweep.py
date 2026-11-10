"""The sweep that fails runs left running after their Modal call ended."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum
from types import SimpleNamespace
from uuid import uuid4

import pytest
from policyengine_simulation_executor.stage12_runtime.run_sweep import (
    DEFAULT_MAXIMUM_AGE,
    CallState,
    SweepSummary,
    modal_call_state,
    sweep_stuck_runs,
)
from policyengine_stage12_persistence import ActiveRun, RunKind, RunStatus, RunWrite

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
    def __init__(self, runs, *, expired=(), maximum_age=DEFAULT_MAXIMUM_AGE):
        self.runs = list(runs)
        # Runs older than the maximum age, which the sweep lists first, at
        # exactly this cutoff; every other listing is for the minimum age.
        self.expired = list(expired)
        self.maximum_age_cutoff = NOW - maximum_age
        self.cutoffs = []
        self.expired_cutoffs = []
        self.failed = []
        self.events = []

    def list_active_runs(self, kind, *, active_since_before):
        # As the real store, a run this fake failed is no longer listed.
        failed = {failed_run for failed_run, _, _ in self.failed}
        if active_since_before == self.maximum_age_cutoff:
            self.expired_cutoffs.append(active_since_before)
            self.events.append(f"list-expired:{kind.value}")
            listed = self.expired
        else:
            self.cutoffs.append(active_since_before)
            self.events.append(f"list:{kind.value}")
            listed = self.runs
        return tuple(run for run in listed if run.kind is kind and run not in failed)

    def fail_stuck_run(self, run, *, error_message, failed_at):
        self.failed.append((run, error_message, failed_at))
        self.events.append(f"fail:{run.kind.value}")
        return RunWrite(changed=True, status=RunStatus.FAILED)


def _run(call_id, kind=RunKind.SIMULATION_RUN, status=RunStatus.RUNNING):
    return ActiveRun(kind=kind, run_id=uuid4(), status=status, modal_call_id=call_id)


def test_a_pending_run_with_no_call_is_failed_without_asking_modal() -> None:
    # The simulation service stopped between inserting the run and starting
    # its call, so nothing will run it.
    never_started = _run(None, RunKind.REPORT_RUN, RunStatus.PENDING)
    # A running run with no call recorded is left to the maximum age.
    running_without_call = _run(None)
    store = FakeSweepStore([never_started, running_without_call])
    asked = []

    summary = sweep_stuck_runs(store, call_state=asked.append, now=NOW)

    assert asked == []
    assert [run for run, _, _ in store.failed] == [never_started]
    (_, message, failed_at) = store.failed[0]
    assert message.startswith("run_not_started: ")
    assert failed_at == NOW
    assert summary == SweepSummary(
        checked=2,
        failed=0,
        running=0,
        unknown=1,
        left=0,
        failed_for_age=0,
        failed_without_call=1,
    )


def test_a_pending_run_with_a_call_is_judged_by_its_call() -> None:
    # The simulation service recorded the call it started; Modal may still
    # be queueing it, or it may have ended without claiming the run.
    queued = _run("fc-queued", RunKind.REPORT_RUN, RunStatus.PENDING)
    ended = _run("fc-ended", RunKind.REPORT_RUN, RunStatus.PENDING)
    store = FakeSweepStore([queued, ended])
    states = {"fc-queued": CallState.RUNNING, "fc-ended": CallState.ENDED}

    summary = sweep_stuck_runs(store, call_state=states.__getitem__, now=NOW)

    assert [run for run, _, _ in store.failed] == [ended]
    assert store.failed[0][1].startswith("modal_call_ended: ")
    assert (summary.checked, summary.failed, summary.running) == (2, 1, 1)


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
    assert summary == SweepSummary(
        checked=3, failed=1, running=1, unknown=1, left=0, failed_for_age=0
    )


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
    # A simulation run is listed only once no report run naming it is
    # running, so failing a dead coordinator's report run first lets the same
    # sweep reach its children.
    report_run = _run("fc-coordinator", RunKind.REPORT_RUN)
    simulation_run = _run("fc-worker")
    store = FakeSweepStore([simulation_run, report_run])

    sweep_stuck_runs(store, call_state=lambda _: CallState.ENDED, now=NOW)

    assert store.events == [
        "list-expired:report_run",
        "list-expired:simulation_run",
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
    assert summary == SweepSummary(
        checked=1, failed=0, running=0, unknown=1, left=0, failed_for_age=0
    )


def test_a_run_another_sweep_failed_first_is_not_counted_again() -> None:
    run = _run("fc-ended")

    class AlreadyFailedStore(FakeSweepStore):
        def fail_stuck_run(self, run, *, error_message, failed_at):
            super().fail_stuck_run(
                run, error_message=error_message, failed_at=failed_at
            )
            return RunWrite(changed=False, status=RunStatus.FAILED)

    summary = sweep_stuck_runs(
        AlreadyFailedStore([run]), call_state=lambda _: CallState.ENDED, now=NOW
    )

    assert summary.failed == 0
    # It finished, or moved to another call, after it was listed.
    assert summary.left == 1


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


def test_a_run_past_the_maximum_age_is_failed_whatever_modal_reports() -> None:
    # Modal reports a call it won't retry after an internal failure as
    # pending, and no longer describes old calls; neither may keep a run
    # running for good, and nor may a run with no recorded call.
    reported_running = _run("fc-reported-running", RunKind.REPORT_RUN)
    # A pending run counts from its creation; its call may still be queued.
    still_queued = _run("fc-queued", RunKind.REPORT_RUN, RunStatus.PENDING)
    without_call = ActiveRun(
        kind=RunKind.REPORT_RUN,
        run_id=uuid4(),
        status=RunStatus.RUNNING,
        modal_call_id=None,
    )
    store = FakeSweepStore(
        [reported_running], expired=[reported_running, still_queued, without_call]
    )

    summary = sweep_stuck_runs(
        store,
        call_state=lambda _: CallState.RUNNING,
        now=NOW,
        maximum_age=timedelta(hours=24),
    )

    assert [run for run, _, _ in store.failed] == [
        reported_running,
        still_queued,
        without_call,
    ]
    assert all(
        message.startswith("run_exceeded_maximum_age: ")
        for _, message, _ in store.failed
    )
    assert summary.failed_for_age == 3
    # Failed for its age first, it is no longer listed for a lookup.
    assert summary.running == 0


def test_an_unrecognised_input_status_leaves_the_call_unknown() -> None:
    unrecognised = SimpleNamespace(name="SOMETHING_NEW")
    graph = [_node("fc-1", unrecognised)]

    assert (
        modal_call_state("fc-1", modal_module=_fake_modal(graph=graph))
        is CallState.UNKNOWN
    )


def test_the_age_limit_is_checked_first_with_its_own_cutoff() -> None:
    # Before any Modal lookup, so lookups that stall can't keep it from
    # running within the scheduled function's time limit.
    store = FakeSweepStore([_run("fc-1", RunKind.REPORT_RUN)])
    asked = []

    def call_state(call_id):
        asked.append((call_id, list(store.events)))
        return CallState.RUNNING

    sweep_stuck_runs(
        store,
        call_state=call_state,
        now=NOW,
        minimum_age=timedelta(minutes=10),
        maximum_age=timedelta(hours=24),
    )

    assert store.expired_cutoffs == [NOW - timedelta(hours=24)] * 2
    assert store.cutoffs == [NOW - timedelta(minutes=10)] * 2
    # Both kinds' age checks come before any lookup.
    assert asked == [
        (
            "fc-1",
            [
                "list-expired:report_run",
                "list-expired:simulation_run",
                "list:report_run",
            ],
        )
    ]
