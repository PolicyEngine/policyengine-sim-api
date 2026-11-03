"""The sweep that fails report runs and simulation runs whose Modal call ended.

A coordinator or worker records the Modal call running each run. If that call
ends without recording the run's outcome, for example because its container
ran out of memory, the run stays running. The sweep asks Modal for the status
of each recorded call and marks the run failed only once the call has ended.

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
        self, kind: RunKind, *, started_before: datetime
    ) -> tuple[RunningRun, ...]:
        """Return the runs of ``kind`` running under a recorded call since a time."""
        ...

    def fail_run_of_ended_call(
        self, run: RunningRun, *, error_message: str, failed_at: datetime
    ) -> RunWrite:
        """Mark ``run`` failed while it is still running under its listed call."""
        ...


@dataclass(frozen=True)
class SweepSummary:
    """How many listed runs the sweep checked, failed, and left for each reason."""

    checked: int
    failed: int
    running: int
    unknown: int


def modal_call_state(modal_call_id: str, *, modal_module: Any = None) -> CallState:  # noqa: ANN401  # the modal module, or a stand-in for it
    """Return whether Modal reports the call as running or ended.

    Modal returns the whole call graph the call is part of, where a call
    spawned inside another call's input is nested under it, so the call's own
    inputs are found by their call ID anywhere in the graph. A call whose
    inputs are all finished has ended; one with any pending input is running.
    A call missing from its graph, or any error asking, is unknown, so the
    sweep leaves its run alone.
    """
    modal = modal_module if modal_module is not None else import_module("modal")
    try:
        graph = modal.FunctionCall.from_id(modal_call_id).get_call_graph()
        statuses = {
            input_info.status.name for input_info in _inputs_of(graph, modal_call_id)
        }
    except Exception as error:  # noqa: BLE001  # any failure to ask leaves the run as it is
        logger.warning(
            "Could not read the status of Modal call %s (%s)",
            modal_call_id,
            type(error).__name__,
        )
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


def sweep_stuck_runs(
    store: StuckRunStore,
    *,
    call_state: Callable[[str], CallState] = modal_call_state,
    now: datetime | None = None,
    minimum_age: timedelta = DEFAULT_MINIMUM_AGE,
) -> SweepSummary:
    """Fail every listed run whose recorded Modal call has ended.

    Report runs are swept first, so a dead coordinator's simulation runs are
    listed in the same sweep. Each call is asked about once. A run whose call
    is running, or whose state is unknown, is left as it is; so is one that
    finished or moved to another call since it was listed, which makes two
    sweeps at once safe.
    """
    now = now or datetime.now(UTC)
    states: dict[str, CallState] = {}
    checked = failed = running = unknown = 0
    for kind in (RunKind.REPORT_RUN, RunKind.SIMULATION_RUN):
        runs = store.list_running_runs(kind, started_before=now - minimum_age)
        checked += len(runs)
        outcome = _sweep_runs(
            store, runs, states=states, call_state=call_state, now=now
        )
        failed += outcome.failed
        running += outcome.running
        unknown += outcome.unknown
    return SweepSummary(
        checked=checked, failed=failed, running=running, unknown=unknown
    )


def _sweep_runs(
    store: StuckRunStore,
    runs: tuple[RunningRun, ...],
    *,
    states: dict[str, CallState],
    call_state: Callable[[str], CallState],
    now: datetime,
) -> SweepSummary:
    """Fail each of ``runs`` whose call has ended, asking about each call once."""
    failed = running = unknown = 0
    for run in runs:
        if run.modal_call_id not in states:
            states[run.modal_call_id] = _safe_call_state(call_state, run.modal_call_id)
        state = states[run.modal_call_id]
        if state is CallState.RUNNING:
            running += 1
        elif state is CallState.UNKNOWN:
            unknown += 1
        elif store.fail_run_of_ended_call(
            run, error_message=ENDED_CALL_MESSAGE, failed_at=now
        ).changed:
            failed += 1
    return SweepSummary(
        checked=len(runs), failed=failed, running=running, unknown=unknown
    )


def _safe_call_state(
    call_state: Callable[[str], CallState], modal_call_id: str
) -> CallState:
    try:
        return call_state(modal_call_id)
    except Exception as error:  # noqa: BLE001  # a failed lookup must never fail the run
        logger.warning(
            "Could not read the status of Modal call %s (%s)",
            modal_call_id,
            type(error).__name__,
        )
        return CallState.UNKNOWN
