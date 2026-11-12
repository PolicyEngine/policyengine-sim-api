"""The stuck-run sweep finishes budget windows that their years no longer advance.

A budget window's years are report runs naming its budget-window run. Each
year's coordinator starts the next waiting years and marks the window when
its year ends; when the sweep fails a year instead, it fails the window
too, and it settles a window that no year is advancing any more.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from policyengine_simulation_executor.stage12_runtime.run_sweep import (
    DEFAULT_MINIMUM_AGE,
    CallState,
    SweepSummary,
    sweep_stuck_runs,
)
from policyengine_stage12_persistence import (
    ActiveRun,
    RunKind,
    RunStatus,
    RunWrite,
    WindowAdvance,
)

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)


class FakeRunStore:
    """Lists ``runs`` for the minimum age and nothing for the maximum age."""

    def __init__(self, runs, *, unchanged=()):
        self.runs = list(runs)
        self.unchanged = set(unchanged)
        self.failed = []

    def list_active_runs(self, kind, *, active_since_before):
        if active_since_before != NOW - DEFAULT_MINIMUM_AGE:
            return ()
        failed = {run for run, _ in self.failed}
        return tuple(run for run in self.runs if run.kind is kind and run not in failed)

    def fail_stuck_run(self, run, *, error_message, failed_at):
        if run.run_id in self.unchanged:
            return RunWrite(changed=False, status=RunStatus.SUCCEEDED)
        self.failed.append((run, error_message))
        return RunWrite(changed=True, status=RunStatus.FAILED)


class FakeWindowStore:
    """Records each window the sweep advances or settles."""

    def __init__(self, *, idle=(), settled=None, missing=()):
        self.idle = tuple(idle)
        self.settled_outcomes = settled or {}
        self.missing = set(missing)
        self.advanced = []
        self.idle_cutoffs = []
        self.settled = []

    def advance_window(self, window_run_id, *, now, start_year=None):
        self.advanced.append((window_run_id, now, start_year))
        if window_run_id in self.missing:
            msg = f"budget-window run {window_run_id} does not exist"
            raise LookupError(msg)
        return WindowAdvance(status=RunStatus.FAILED, changed=True)

    def list_idle_windows(self, *, idle_since_before):
        self.idle_cutoffs.append(idle_since_before)
        return self.idle

    def settle_idle_window(self, window_run_id, *, idle_since_before, now):
        self.settled.append((window_run_id, idle_since_before, now))
        if window_run_id in self.missing:
            msg = f"budget-window run {window_run_id} does not exist"
            raise LookupError(msg)
        return self.settled_outcomes.get(
            window_run_id, WindowAdvance(status=RunStatus.RUNNING, changed=False)
        )


def _year_run(call_id, window_run_id, status=RunStatus.RUNNING):
    return ActiveRun(
        kind=RunKind.REPORT_RUN,
        run_id=uuid4(),
        status=status,
        modal_call_id=call_id,
        budget_window_run_id=window_run_id,
    )


def test_failing_a_year_fails_its_window_and_starts_nothing() -> None:
    window_run_id = uuid4()
    ended_year = _year_run("fc-ended", window_run_id)
    other_report = ActiveRun(
        kind=RunKind.REPORT_RUN,
        run_id=uuid4(),
        status=RunStatus.RUNNING,
        modal_call_id="fc-other",
    )
    runs = FakeRunStore([ended_year, other_report])
    windows = FakeWindowStore()

    summary = sweep_stuck_runs(
        runs,
        windows=windows,
        call_state=lambda call_id: CallState.ENDED,
        now=NOW,
    )

    assert windows.advanced == [(window_run_id, NOW, None)]
    assert summary.failed == 2
    assert summary.windows_failed == 1


def test_a_year_the_sweep_did_not_change_leaves_its_window_alone() -> None:
    year = _year_run("fc-ended", uuid4())
    runs = FakeRunStore([year], unchanged={year.run_id})
    windows = FakeWindowStore()

    summary = sweep_stuck_runs(
        runs,
        windows=windows,
        call_state=lambda call_id: CallState.ENDED,
        now=NOW,
    )

    assert windows.advanced == []
    assert (summary.left, summary.windows_failed) == (1, 0)


def test_idle_windows_are_settled_after_the_runs() -> None:
    stalled, finished, busy = uuid4(), uuid4(), uuid4()
    windows = FakeWindowStore(
        idle=(stalled, finished, busy),
        settled={
            stalled: WindowAdvance(status=RunStatus.FAILED, changed=True),
            finished: WindowAdvance(status=RunStatus.SUCCEEDED, changed=True),
        },
    )

    summary = sweep_stuck_runs(
        FakeRunStore([]), windows=windows, call_state=lambda _: CallState.ENDED, now=NOW
    )

    cutoff = NOW - DEFAULT_MINIMUM_AGE
    assert windows.idle_cutoffs == [cutoff]
    assert windows.settled == [
        (stalled, cutoff, NOW),
        (finished, cutoff, NOW),
        (busy, cutoff, NOW),
    ]
    assert (summary.windows_failed, summary.windows_succeeded) == (1, 1)


def test_a_window_deleted_meanwhile_is_skipped() -> None:
    gone = uuid4()
    year = _year_run("fc-ended", gone)
    windows = FakeWindowStore(idle=(gone,), missing={gone})

    summary = sweep_stuck_runs(
        FakeRunStore([year]),
        windows=windows,
        call_state=lambda _: CallState.ENDED,
        now=NOW,
    )

    assert summary.failed == 1
    assert (summary.windows_failed, summary.windows_succeeded) == (0, 0)


def test_without_a_window_store_the_sweep_judges_runs_only() -> None:
    year = _year_run(None, uuid4(), status=RunStatus.PENDING)

    summary = sweep_stuck_runs(
        FakeRunStore([year]), call_state=lambda _: CallState.ENDED, now=NOW
    )

    assert summary == SweepSummary(
        checked=1,
        failed=0,
        running=0,
        unknown=0,
        failed_without_call=1,
    )


def test_the_window_cutoff_is_the_minimum_age() -> None:
    windows = FakeWindowStore()

    sweep_stuck_runs(
        FakeRunStore([]),
        windows=windows,
        call_state=lambda _: CallState.ENDED,
        now=NOW,
        minimum_age=timedelta(minutes=25),
    )

    assert windows.idle_cutoffs == [NOW - timedelta(minutes=25)]
