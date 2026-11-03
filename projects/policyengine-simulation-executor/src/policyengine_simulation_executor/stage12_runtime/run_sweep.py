"""The sweep that fails report runs and simulation runs whose Modal call ended.

A coordinator or worker records the Modal call running each run. If that call
ends without recording the run's outcome, for example because its container
ran out of memory, the run stays running. The sweep asks Modal for the status
of each recorded call and marks the run failed once the call has ended. It
also fails a run still running long after it started, longer than any Modal
call of it can run, whatever Modal reports and with or without a recorded
call, so a run Modal can't decide doesn't stay running for good.

Modal restarts a pre-empted call under the same call ID and reports it
pending meanwhile, so a pre-empted coordinator's report run is never failed.
Its children are cancelled and started again under new calls, so the sweep
judges a simulation run only once no report run combining it is running.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol

from policyengine_stage12_persistence import RunKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from policyengine_stage12_persistence import RunningRun, RunWrite

logger = logging.getLogger(__name__)

# A run must have been running at least this long, counted from its first
# claim, before the sweep checks its call.
DEFAULT_MINIMUM_AGE = timedelta(minutes=10)
ENDED_CALL_MESSAGE = (
    "modal_call_ended: The Modal call recorded for this run ended without "
    "recording the run's outcome."
)
# A run still running this long after its first claim is failed whatever
# Modal reports, or with no call recorded at all. No attempt of the v2
# functions can run anywhere near this long (the coordinator's own time
# limit is 90 minutes), however often Modal pre-empts and restarts it; it
# stops Modal's pending status for an internal failure it won't retry, or
# a call Modal no longer describes, from keeping a run running for good.
DEFAULT_MAXIMUM_AGE = timedelta(hours=24)
EXCEEDED_AGE_MESSAGE = (
    "run_exceeded_maximum_age: The run was still running long after it "
    "started, longer than any Modal call of it can run."
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

    def list_running_runs(
        self,
        kind: RunKind,
        *,
        started_before: datetime,
        include_runs_without_call: bool = False,
    ) -> tuple[RunningRun, ...]:
        """Return the runs of ``kind`` running since before a time."""
        ...

    def fail_stuck_run(
        self, run: RunningRun, *, error_message: str, failed_at: datetime
    ) -> RunWrite:
        """Mark ``run`` failed while it is as it was listed.

        It must still be running under its listed call, or with no call if it
        was listed with none; a simulation run must still have no running
        report run combining it.
        """
        ...


@dataclass(frozen=True)
class SweepSummary:
    """What the sweep did with the runs it listed.

    ``checked`` runs had their call asked about: ``failed`` because it ended,
    or left because it is ``running`` or its state is ``unknown``.
    ``failed_for_age`` runs were failed for exceeding the maximum age. ``left``
    counts listed runs whose conditional failure changed nothing, because
    they finished, moved to another call, or a running report run started
    combining them after they were listed.
    """

    checked: int
    failed: int
    running: int
    unknown: int
    left: int = 0
    failed_for_age: int = 0


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


def sweep_stuck_runs(  # the store, then keyword-only seams and limits
    store: StuckRunStore,
    *,
    call_state: Callable[[str], CallState] = modal_call_state,
    now: datetime | None = None,
    minimum_age: timedelta = DEFAULT_MINIMUM_AGE,
    maximum_age: timedelta = DEFAULT_MAXIMUM_AGE,
) -> SweepSummary:
    """Fail every listed run whose recorded Modal call has ended.

    Runs older than ``maximum_age`` are failed first, report runs then
    simulation runs, before any Modal lookup can use up the scheduled
    function's time. Then each recorded call is asked about once, report
    runs first, so a dead coordinator's simulation runs are listed in the
    same sweep. A run whose call is running, or whose state is unknown, is
    left as it is; so is one whose conditional failure changes nothing, which
    makes two sweeps at once safe.
    """
    now = now or datetime.now(UTC)
    states: dict[str, CallState] = {}
    tally = _Tally()
    for kind in (RunKind.REPORT_RUN, RunKind.SIMULATION_RUN):
        for run in store.list_running_runs(
            kind, started_before=now - maximum_age, include_runs_without_call=True
        ):
            if store.fail_stuck_run(
                run, error_message=EXCEEDED_AGE_MESSAGE, failed_at=now
            ).changed:
                tally.failed_for_age += 1
            else:
                tally.left += 1
    for kind in (RunKind.REPORT_RUN, RunKind.SIMULATION_RUN):
        for run in store.list_running_runs(kind, started_before=now - minimum_age):
            _sweep_run(
                store, run, states=states, call_state=call_state, now=now, tally=tally
            )
    return SweepSummary(**vars(tally))


def _sweep_run(  # noqa: PLR0913  # one run, the store, and the sweep's shared state
    store: StuckRunStore,
    run: RunningRun,
    *,
    states: dict[str, CallState],
    call_state: Callable[[str], CallState],
    now: datetime,
    tally: _Tally,
) -> None:
    """Fail ``run`` if its call has ended, asking about each call once."""
    tally.checked += 1
    modal_call_id = run.modal_call_id
    if modal_call_id is None:
        tally.unknown += 1
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
    elif store.fail_stuck_run(
        run, error_message=ENDED_CALL_MESSAGE, failed_at=now
    ).changed:
        tally.failed += 1
    else:
        tally.left += 1


def _log_unreadable_call(modal_call_id: str, error: Exception) -> None:
    logger.warning(
        "Could not read the status of Modal call %s (%s)",
        modal_call_id,
        type(error).__name__,
    )
