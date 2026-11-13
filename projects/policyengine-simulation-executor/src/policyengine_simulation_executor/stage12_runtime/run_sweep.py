"""The sweep that fails runs and household jobs nothing will finish.

A coordinator or worker records the Modal call running each run it claims,
and the simulation service is to record the call it starts on a pending
report run, or on a simulation run it starts a worker on directly. If that
call ends without recording the run's outcome, for example because its
container ran out of memory, the run stays pending or running. The sweep
asks Modal for the status of each recorded call and marks the run failed
once the call has ended. A pending run with no call recorded soon after its
creation is failed without asking Modal: usually the simulation service
stopped before starting its call. If it started the call but failed to
record it, that call's claim is refused once the run has failed, so it
finishes nothing. The sweep also fails a run still unfinished long after it
was created or started, longer than any Modal call of it can run, whatever
Modal reports and with or without a recorded call, so a run Modal can't
decide doesn't stay unfinished for good.

Modal restarts a pre-empted call under the same call ID and reports it
pending meanwhile, so a pre-empted coordinator's or worker's run is never
failed. Every coordinator of a pending or running report run that names a
simulation run, as its baseline or reform, waits for the call holding it and
fails it once that call ended without an outcome; a pre-empted coordinator's
workers keep running meanwhile. So the sweep judges a simulation run by its
call only once no pending or running report run names it. Its age limit
applies whatever report runs name a run: new ones may keep joining, and
waiting for, a run whose call Modal never ends.

Household jobs follow the same rule: the simulation service records the
call it starts on a pending job, and a household function claims the job
under that call. A household job counts from its creation, while pending or
running, for both ages.

A budget window's years are report runs naming its budget-window run. A
year still pending with no call, in a window that hasn't finished, waits
for an earlier year's coordinator to start it, so the sweep leaves it to the
window. A year started later counts, until its claim, from when its call
was recorded rather than from its creation at submit. When the sweep fails
a year, it fails the window too, with the window's waiting years. After
judging the runs, it settles each unfinished window that nothing has
changed for the minimum age with no year under way: its last year ended,
or the simulation service created it, without anything recording what
follows. Such a window is finished from its years, or failed as stalled
with the years still waiting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol

from policyengine_stage12_persistence import RunKind, RunStatus

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from policyengine_stage12_persistence import ActiveRun, RunWrite, WindowAdvance

logger = logging.getLogger(__name__)

# A run must have been unfinished at least this long, counted from its first
# claim or, while it is pending, from its creation (a household job's from its
# creation), before the sweep judges it; a pending run with no call recorded
# by then is failed.
DEFAULT_MINIMUM_AGE = timedelta(minutes=10)
ENDED_CALL_MESSAGE = (
    "modal_call_ended: The Modal call recorded for this run ended without "
    "recording the run's outcome."
)
NOT_STARTED_MESSAGE = (
    "run_not_started: The run stayed pending with no Modal call recorded for it."
)
# A run still unfinished this long after its first claim, or its creation if
# it was never claimed (a household job's creation), is failed whatever Modal
# reports, or with no call recorded at all. No attempt of the v2
# functions can run anywhere near this long (the coordinator's own time
# limit is 90 minutes), however often Modal pre-empts and restarts it; it
# stops Modal's pending status for an internal failure it won't retry, or
# a call Modal no longer describes, from keeping a run running for good.
DEFAULT_MAXIMUM_AGE = timedelta(hours=24)
EXCEEDED_AGE_MESSAGE = (
    "run_exceeded_maximum_age: The run was still unfinished long after it "
    "was created or started, longer than any Modal call of it can run."
)
# Modal's input statuses (``modal.call_graph.InputStatus``) by name. A pending
# input is running or waiting to run again; Modal reports an input pre-empted
# and waiting to restart (internally INTERNAL_FAILURE) as pending. Any other
# name leaves the call's state unknown.
RUNNING_INPUT_STATUSES = frozenset({"PENDING"})
ENDED_INPUT_STATUSES = frozenset(
    {"SUCCESS", "FAILURE", "TERMINATED", "TIMEOUT", "INIT_FAILURE"}
)


class CallState(StrEnum):
    """What Modal reports about a recorded call."""

    RUNNING = "running"
    ENDED = "ended"
    UNKNOWN = "unknown"


class StuckRunStore(Protocol):
    """The sweep's reads and writes; ``ReportRunStore`` implements them."""

    def list_active_runs(
        self,
        kind: RunKind,
        *,
        active_since_before: datetime,
        regardless_of_report_runs: bool = False,
    ) -> tuple[ActiveRun, ...]:
        """Return the pending and running runs, or household jobs, older than a time.

        A simulation run an unfinished report run names is left out, unless
        ``regardless_of_report_runs``.
        """
        ...

    def fail_stuck_run(
        self,
        run: ActiveRun,
        *,
        error_message: str,
        failed_at: datetime,
        regardless_of_report_runs: bool = False,
    ) -> RunWrite:
        """Mark ``run`` failed while it is as it was listed.

        It must still have its listed status and call, or no call if it was
        listed with none; a simulation run must still have no pending or
        running report run naming it, unless ``regardless_of_report_runs``.
        """
        ...


class BudgetWindowSettler(Protocol):
    """The sweep's budget-window writes; ``BudgetWindowStore`` implements them."""

    def advance_window(self, window_run_id: UUID, *, now: datetime) -> WindowAdvance:
        """Finish the window from its years' outcomes; start no year."""
        ...

    def list_idle_windows(self, *, idle_since_before: datetime) -> tuple[UUID, ...]:
        """Return the unfinished windows with no year under way, unchanged since."""
        ...

    def settle_idle_window(
        self, window_run_id: UUID, *, idle_since_before: datetime, now: datetime
    ) -> WindowAdvance:
        """Finish a window still idle: from its years, or as stalled."""
        ...


# The order the sweep judges each kind in: report runs before simulation
# runs, which are listed only once no unfinished report run names them, then
# household jobs, which no run names.
SWEPT_KINDS = (RunKind.REPORT_RUN, RunKind.SIMULATION_RUN, RunKind.HOUSEHOLD_JOB)


@dataclass(frozen=True)
class SweepSummary:
    """What the sweep did with the runs and household jobs it listed, together.

    ``checked`` runs were judged after the minimum age: ``failed`` because
    their call ended, ``failed_without_call`` because they stayed pending with
    no call recorded, or left because their call is ``running`` or its state
    is ``unknown``, as is a running run with no call recorded.
    ``failed_for_age`` runs were failed for exceeding the maximum age. ``left``
    counts listed runs whose conditional failure changed nothing, because
    they were claimed, finished, moved to another call, had a call recorded,
    or a pending or running report run named them after they were listed.
    ``windows_failed`` and ``windows_succeeded`` count the budget windows the
    sweep finished, after failing one of their years or finding them idle.
    """

    checked: int
    failed: int
    running: int
    unknown: int
    left: int = 0
    failed_for_age: int = 0
    failed_without_call: int = 0
    windows_failed: int = 0
    windows_succeeded: int = 0


def modal_call_state(modal_call_id: str, *, modal_module: Any = None) -> CallState:  # noqa: ANN401  # the modal module, or a stand-in for it
    """Return whether Modal reports the call as running or ended.

    Modal returns the whole call graph the call is part of, where a call
    spawned inside another call's input is nested under it, so the call's own
    inputs are found by their call ID anywhere in the graph. A call whose
    inputs are all finished has ended; one with any pending input is running.
    A call missing from its graph, a status this code doesn't know, or any
    error asking, is unknown, so the sweep leaves its run alone.
    """
    modal = modal_module if modal_module is not None else import_module("modal")
    try:
        graph = modal.FunctionCall.from_id(modal_call_id).get_call_graph()
        statuses = {
            input_info.status.name for input_info in _inputs_of(graph, modal_call_id)
        }
    except Exception as error:  # noqa: BLE001  # any failure to ask leaves the run as it is
        _log_unreadable_call(modal_call_id, error)
        return CallState.UNKNOWN
    if statuses & RUNNING_INPUT_STATUSES:
        return CallState.RUNNING
    if statuses and statuses <= ENDED_INPUT_STATUSES:
        return CallState.ENDED
    return CallState.UNKNOWN


def _inputs_of(graph: list[Any], modal_call_id: str) -> list[Any]:
    """Return every input in ``graph`` that belongs to call ``modal_call_id``."""
    found: list[Any] = []
    pending = list(graph)
    while pending:
        input_info = pending.pop()
        if input_info.function_call_id == modal_call_id:
            found.append(input_info)
        pending.extend(input_info.children)
    return found


@dataclass
class _Tally:
    """Running counts of one sweep."""

    checked: int = 0
    failed: int = 0
    running: int = 0
    unknown: int = 0
    left: int = 0
    failed_for_age: int = 0
    failed_without_call: int = 0
    windows_failed: int = 0
    windows_succeeded: int = 0

    def count_window(self, advance: WindowAdvance | None) -> None:
        """Count a window the sweep finished; ``None`` means it was missing."""
        if advance is None or not advance.changed:
            return
        if advance.status is RunStatus.FAILED:
            self.windows_failed += 1
        elif advance.status is RunStatus.SUCCEEDED:
            self.windows_succeeded += 1


@dataclass(frozen=True)
class _SweepPass:
    """One sweep's shared state: its stores, its clock and its counts."""

    store: StuckRunStore
    windows: BudgetWindowSettler | None
    now: datetime
    tally: _Tally

    def fail(
        self,
        run: ActiveRun,
        error_message: str,
        *,
        regardless_of_report_runs: bool = False,
    ) -> bool:
        """Fail ``run`` as listed, then its window if it is a window's year.

        Returns whether the run changed.
        """
        changed = self.store.fail_stuck_run(
            run,
            error_message=error_message,
            failed_at=self.now,
            regardless_of_report_runs=regardless_of_report_runs,
        ).changed
        if changed and run.budget_window_run_id is not None and self.windows:
            windows = self.windows
            self.tally.count_window(
                _unless_missing(
                    run.budget_window_run_id,
                    lambda window_run_id: windows.advance_window(
                        window_run_id, now=self.now
                    ),
                )
            )
        return changed

    def settle_idle_windows(self, idle_since_before: datetime) -> None:
        """Finish each window no year has advanced since ``idle_since_before``."""
        if self.windows is None:
            return
        windows = self.windows
        for window_run_id in windows.list_idle_windows(
            idle_since_before=idle_since_before
        ):
            self.tally.count_window(
                _unless_missing(
                    window_run_id,
                    lambda idle_window_run_id: windows.settle_idle_window(
                        idle_window_run_id,
                        idle_since_before=idle_since_before,
                        now=self.now,
                    ),
                )
            )


def _unless_missing(
    window_run_id: UUID, finish: Callable[[UUID], WindowAdvance]
) -> WindowAdvance | None:
    """Finish the window, or log and return None when it no longer exists."""
    try:
        return finish(window_run_id)
    except LookupError:
        logger.info("Budget-window run %s no longer exists", window_run_id)
        return None


def sweep_stuck_runs(  # noqa: PLR0913  # the stores, then keyword-only seams and limits
    store: StuckRunStore,
    *,
    windows: BudgetWindowSettler | None = None,
    call_state: Callable[[str], CallState] = modal_call_state,
    now: datetime | None = None,
    minimum_age: timedelta = DEFAULT_MINIMUM_AGE,
    maximum_age: timedelta = DEFAULT_MAXIMUM_AGE,
) -> SweepSummary:
    """Fail every listed run whose recorded Modal call has ended or was never started.

    Household jobs are judged as runs are. Runs older than ``maximum_age``
    are failed first, report runs, then simulation runs, then household
    jobs, before any Modal lookup can use up the scheduled function's time.
    Then, in the same order, so a dead coordinator's simulation runs are
    listed in the same sweep, each run older than ``minimum_age`` is judged:
    a pending run with no call recorded is failed, and each recorded call is
    asked about once. A run whose call is running,
    or whose state is unknown, is left as it is, as is a running run with no
    call recorded, until the maximum age; so is one whose conditional failure
    changes nothing, which makes two sweeps at once safe.

    With ``windows``, a failed year of a budget window fails its window, and
    the windows no year has advanced for ``minimum_age`` are then settled.
    """
    now = now or datetime.now(UTC)
    states: dict[str, CallState] = {}
    sweep = _SweepPass(store=store, windows=windows, now=now, tally=_Tally())
    tally = sweep.tally
    for kind in SWEPT_KINDS:
        # Shared simulation runs: report runs may keep joining, and waiting
        # for, a run whose call Modal never ends, so the age limit ignores
        # the report runs that name a run.
        for run in store.list_active_runs(
            kind, active_since_before=now - maximum_age, regardless_of_report_runs=True
        ):
            if sweep.fail(run, EXCEEDED_AGE_MESSAGE, regardless_of_report_runs=True):
                tally.failed_for_age += 1
            else:
                tally.left += 1
    for kind in SWEPT_KINDS:
        for run in store.list_active_runs(kind, active_since_before=now - minimum_age):
            _sweep_run(sweep, run, states=states, call_state=call_state)
    sweep.settle_idle_windows(now - minimum_age)
    return SweepSummary(**vars(tally))


def _sweep_run(
    sweep: _SweepPass,
    run: ActiveRun,
    *,
    states: dict[str, CallState],
    call_state: Callable[[str], CallState],
) -> None:
    """Fail ``run`` if its call has ended or was never started.

    Each call is asked about once.
    """
    tally = sweep.tally
    tally.checked += 1
    modal_call_id = run.modal_call_id
    if modal_call_id is None:
        if run.status is not RunStatus.PENDING:
            # Nothing to ask Modal about; the maximum age decides it.
            tally.unknown += 1
        elif sweep.fail(run, NOT_STARTED_MESSAGE):
            tally.failed_without_call += 1
        else:
            tally.left += 1
        return
    if modal_call_id not in states:
        try:
            states[modal_call_id] = call_state(modal_call_id)
        except Exception as error:  # noqa: BLE001  # a failed lookup must never fail the run
            _log_unreadable_call(modal_call_id, error)
            states[modal_call_id] = CallState.UNKNOWN
    state = states[modal_call_id]
    if state is CallState.RUNNING:
        tally.running += 1
    elif state is CallState.UNKNOWN:
        tally.unknown += 1
    elif sweep.fail(run, ENDED_CALL_MESSAGE):
        tally.failed += 1
    else:
        tally.left += 1


def _log_unreadable_call(modal_call_id: str, error: Exception) -> None:
    logger.warning(
        "Could not read the status of Modal call %s (%s)",
        modal_call_id,
        type(error).__name__,
    )
