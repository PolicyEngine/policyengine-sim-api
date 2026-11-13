"""Simulation runs a report run's coordinator waits for while another call holds them.

Identical requests share simulation runs, so a report run may name a run that
another call is calculating: another report run's worker, a worker this report
run's earlier, pre-empted attempt started, or a worker the simulation service
started on the run itself. The coordinator never cancels or releases that call.
A ``WaitedRun`` stands in for it in the coordinator's wait: each probe asks the
holding call for its result without waiting, and once the call has ended, the
run is read again:

- **succeeded:** ``WaitedRunSucceeded``, and the coordinator reads the run's
  stored file and checks it as it does a reused run's;
- **failed:** ``WaitedRunFailedError``, carrying the run's own recorded failure;
- **pending with no call:** ``WaitedRunUnstarted``, and the coordinator starts
  its own worker on it;
- **held by another call:** that call is waited for instead;
- **still unfinished under the call that ended:** the call stopped without
  recording an outcome, so the run is failed while it still records that
  call, with the stuck-run sweep's message, and every coordinator waiting for
  it stops waiting. The sweep itself never judges a run an unfinished report
  run names.

A probe that raises counts as the call ending only once Modal reports the
call ended (``call_state``, the sweep's ``modal_call_state``): Modal's client
raises for a lost connection too, while the call goes on. Until then the
coordinator keeps waiting, within its wait window. ``ProbedWorkerCall`` gives
the coordinator's own workers the same rule.

A household report run's function waits for a side another call holds with
``wait_for_held_run``, which probes a ``WaitedRun`` until its run finishes,
is left unstarted, or the function's own wait window closes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from policyengine_stage12_persistence import RunStatus

from .failures import (
    Stage12ExecutionError,
    Stage12FailureDetail,
    failure_detail_from_record,
)
from .report_run_recording import run_failure_detail
from .run_sweep import ENDED_CALL_MESSAGE, CallState

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from policyengine_stage12_persistence import RunWrite, SimulationRunState

    from .dependencies import ChildCall

# A run that failed recording no failure of its own.
SIMULATION_RUN_FAILED = "simulation_run_failed"


def recorded_run_failure(
    state: SimulationRunState, *, unrecorded_summary: str
) -> Stage12FailureDetail:
    """Return the failure a failed run recorded, whole, so its correlation ID stays.

    A run that recorded none gives ``simulation_run_failed`` with
    ``unrecorded_summary``.
    """
    if state.error_message:
        return run_failure_detail(state.error_message)
    return failure_detail_from_record(
        error_code=SIMULATION_RUN_FAILED, error_summary=unrecorded_summary
    )


@dataclass(frozen=True)
class WaitedRunSucceeded:
    """A waited-for run succeeded; ``state`` is the run as read then."""

    state: SimulationRunState


@dataclass(frozen=True)
class WaitedRunUnstarted:
    """A waited-for run is pending with no call: no worker holds it."""


class WaitedRunFailedError(Stage12ExecutionError):
    """A waited-for run failed; ``detail`` is the failure it recorded."""


class HeldRunStore(Protocol):
    """The reads and the one write a wait for a held run makes."""

    def get_simulation_run(self, simulation_run_id: UUID) -> SimulationRunState:
        """Return the run's state, with its recorded call and failure."""
        ...

    def fail_simulation_run_held_by(
        self,
        simulation_run_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: datetime,
    ) -> RunWrite:
        """Mark the run failed while it is unfinished and records ``modal_call_id``."""
        ...


@dataclass
class WaitedRun:
    """The call holding a simulation run, as a child call the coordinator probes.

    ``object_id`` is the call the run recorded when the wait began, empty for
    a run to read before any call is probed; it follows the run to a new
    holding call. ``restore`` gives the handle of a call by its ID.
    """

    simulation_run_id: UUID
    writer: HeldRunStore
    restore: Callable[[str], ChildCall]
    call_state: Callable[[str], CallState]
    object_id: str = ""
    _holder: ChildCall | None = None

    def get(
        self, *, timeout: float | None = None
    ) -> WaitedRunSucceeded | WaitedRunUnstarted:
        """Return the run's outcome once its holding call has ended.

        Raises TimeoutError while the call runs, or after the run moved to
        another call, which is probed next; ``WaitedRunFailedError`` for a run
        that failed, failing it first if its call ended without an outcome.
        """
        if self.object_id:
            try:
                self._holding_call().get(timeout=0 if timeout is None else timeout)
            except TimeoutError:
                raise
            except Exception as error:
                # Once Modal says the call ended, the run's row says how.
                if self.call_state(self.object_id) is not CallState.ENDED:
                    msg = "probing the holding call failed while it runs"
                    raise TimeoutError(msg) from error
        return self._outcome()

    def cancel(self) -> None:
        """Do nothing: another report run, or the earlier attempt, owns the call."""

    def _holding_call(self) -> ChildCall:
        if self._holder is None:
            self._holder = self.restore(self.object_id)
        return self._holder

    def _follow(self, modal_call_id: str | None) -> None:
        """Probe ``modal_call_id``, the run's new holding call, from now on."""
        self.object_id, self._holder = modal_call_id or "", None

    def _outcome(self) -> WaitedRunSucceeded | WaitedRunUnstarted:
        state = self.writer.get_simulation_run(self.simulation_run_id)
        if state.status in {RunStatus.PENDING, RunStatus.RUNNING}:
            if state.modal_call_id is None:
                return WaitedRunUnstarted()
            if state.modal_call_id != self.object_id:
                self._follow(state.modal_call_id)
                msg = "the waited-for run moved to another call"
                raise TimeoutError(msg)
            self.writer.fail_simulation_run_held_by(
                self.simulation_run_id,
                modal_call_id=state.modal_call_id,
                error_message=ENDED_CALL_MESSAGE,
                failed_at=datetime.now(UTC),
            )
            state = self.writer.get_simulation_run(self.simulation_run_id)
        if state.status is RunStatus.SUCCEEDED:
            return WaitedRunSucceeded(state)
        if state.status is RunStatus.FAILED:
            raise WaitedRunFailedError(
                recorded_run_failure(
                    state,
                    unrecorded_summary=(
                        f"The simulation run {self.simulation_run_id} another "
                        "call calculated failed."
                    ),
                )
            )
        # Another call claimed it between the failure and the read.
        self._follow(state.modal_call_id)
        msg = "the waited-for run moved to another call"
        raise TimeoutError(msg)


@dataclass
class ProbedWorkerCall:
    """A worker call this coordinator started, whose failed probe may be Modal's.

    A probe that raises is passed on only once Modal reports the call ended;
    while Modal reports it running, or can't say, the coordinator keeps
    waiting, so a lost connection never fails a worker's run.
    """

    call: ChildCall
    call_state: Callable[[str], CallState]
    object_id: str = ""

    def __post_init__(self) -> None:
        """Take the worker call's ID."""
        self.object_id = self.call.object_id

    def get(self, *, timeout: float | None = None) -> object:
        """Return the worker's result, or raise as it ended once Modal says it did."""
        try:
            return self.call.get(timeout=timeout)
        except TimeoutError:
            raise
        except Exception as error:
            if self.call_state(self.object_id) is CallState.ENDED:
                raise
            msg = "probing the worker call failed while it runs"
            raise TimeoutError(msg) from error

    def cancel(self) -> None:
        """Ask Modal to stop the worker call."""
        self.call.cancel()


# How often a household function probes a held run: from a quarter of a
# second, doubling while nothing changes, up to every second.
HELD_RUN_POLL_INITIAL_SECONDS = 0.25
HELD_RUN_POLL_MAX_SECONDS = 1.0


class HeldRunWaitTimeoutError(Exception):
    """A held run was still unfinished when the wait window closed."""


@dataclass(frozen=True)
class HeldRunWait:
    """How a function that is no coordinator waits for a run another call holds.

    ``restore`` gives a call's handle by its ID and ``call_state`` Modal's
    state of a call, as for the coordinator; the window is
    ``timeout_seconds`` from the function's start, on ``monotonic``.
    """

    restore: Callable[[str], ChildCall]
    call_state: Callable[[str], CallState]
    timeout_seconds: float
    sleep: Callable[[float], None] = field(default=time.sleep)
    monotonic: Callable[[], float] = field(default=time.monotonic)


def wait_for_held_run(
    waited: WaitedRun, wait: HeldRunWait, *, deadline: float
) -> WaitedRunSucceeded | WaitedRunUnstarted:
    """Probe ``waited`` until its run finishes or is unstarted, until ``deadline``.

    Raises ``WaitedRunFailedError`` for a run that failed, and
    ``HeldRunWaitTimeoutError`` once the monotonic ``deadline`` passes with
    the run still unfinished. Between probes it sleeps, backing off, never
    past the deadline.
    """
    poll_interval = HELD_RUN_POLL_INITIAL_SECONDS
    while True:
        try:
            return waited.get(timeout=0)
        except TimeoutError:
            pass
        remaining_seconds = deadline - wait.monotonic()
        if remaining_seconds <= 0:
            msg = "the held run was still unfinished when the wait window closed"
            raise HeldRunWaitTimeoutError(msg)
        wait.sleep(min(poll_interval, remaining_seconds))
        poll_interval = min(poll_interval * 2, HELD_RUN_POLL_MAX_SECONDS)
