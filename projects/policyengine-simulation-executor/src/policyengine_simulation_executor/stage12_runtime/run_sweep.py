"""The sweep that fails report runs and simulation runs whose Modal call ended.

A coordinator or worker records the Modal call running each run. If that call
ends without recording the run's outcome, for example because its container
ran out of memory, the run stays running. The sweep asks Modal for the status
of each recorded call and marks the run failed only once the call has ended.
Modal restarts a pre-empted call under the same call ID, and reports it
pending meanwhile, so a pre-empted run is never failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from policyengine_stage12_persistence import RunningRun, RunWrite

logger = logging.getLogger(__name__)

# A run must have been running at least this long before the sweep checks
# its call, so it never races the dispatch that records the call.
DEFAULT_MINIMUM_AGE = timedelta(minutes=10)
ENDED_CALL_MESSAGE = (
    "modal_call_ended: The Modal call recorded for this run ended without "
    "recording the run's outcome."
)
# Modal's input statuses (``modal.call_graph.InputStatus``) by name; only a
# pending input is still running or waiting to run again.
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

    def list_running_runs(self, *, started_before: datetime) -> tuple[RunningRun, ...]:
        """Return the runs running under a recorded call since before a time."""
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

    A call whose inputs are all finished has ended; one with any pending input
    is running. A call Modal cannot describe, or any error asking, is unknown,
    so the sweep leaves its run alone.
    """
    modal = modal_module if modal_module is not None else import_module("modal")
    try:
        inputs = modal.FunctionCall.from_id(modal_call_id).get_call_graph()
        statuses = {input_info.status.name for input_info in inputs}
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


def sweep_stuck_runs(
    store: StuckRunStore,
    *,
    call_state: Callable[[str], CallState] = modal_call_state,
    now: datetime | None = None,
    minimum_age: timedelta = DEFAULT_MINIMUM_AGE,
) -> SweepSummary:
    """Fail every listed run whose recorded Modal call has ended.

    Each call is asked about once. A run whose call is running, or whose state
    is unknown, is left as it is; so is one that finished or moved to another
    call since it was listed, which makes two sweeps at once safe.
    """
    now = now or datetime.now(UTC)
    runs = store.list_running_runs(started_before=now - minimum_age)
    states: dict[str, CallState] = {}
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
