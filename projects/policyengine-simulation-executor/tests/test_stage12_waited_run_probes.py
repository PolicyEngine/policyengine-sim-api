"""How a wait for a held simulation run treats a probe of the holding call that raises.

Modal's client raises for a lost connection as well as for a call that ended,
so a failed probe reads the run's row first: a run that finished, or moved to
another call, needs no word from Modal. Only for a run still unfinished under
the probed call does the wait ask Modal for the call's state, and it keeps
waiting unless Modal reports the call ended, including when Modal can't say.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from uuid import UUID, uuid4

import pytest
from policyengine_simulation_executor.stage12_runtime.run_sweep import (
    ENDED_CALL_MESSAGE,
    CallState,
)
from policyengine_simulation_executor.stage12_runtime.shared_simulation_runs import (
    WaitedRun,
    WaitedRunFailedError,
    WaitedRunSucceeded,
    WaitedRunUnstarted,
)
from policyengine_stage12_persistence import RunStatus, RunWrite, SimulationRunState

HOLDING_CALL_ID = "fc-holding-worker"
WAITED_RUN_ID = uuid4()


class LostConnectionCall:
    """A holding call whose every probe fails as a lost connection to Modal does."""

    object_id = HOLDING_CALL_ID

    def get(self, *, timeout: float | None = None) -> object:
        msg = "the connection to Modal was reset"
        raise ConnectionError(msg)

    def cancel(self) -> None:
        """Do nothing: the wait never cancels its holder."""


def _run_state(
    status: RunStatus, *, error_message: str | None = None
) -> SimulationRunState:
    return SimulationRunState(
        status=status,
        year=2026,
        country_package_version="1.0.0",
        policyengine_version="6.12.0",
        dataset_id=None,
        region_id=None,
        output_uri=None,
        output_sha256=None,
        modal_call_id=HOLDING_CALL_ID,
        error_message=error_message,
    )


@dataclass
class OneRunStore:
    """The waited-for run's row, and the calls the wait failed it for."""

    state: SimulationRunState
    failed_for_calls: list[str] = field(default_factory=list)

    def get_simulation_run(self, simulation_run_id: UUID) -> SimulationRunState:
        assert simulation_run_id == WAITED_RUN_ID
        return self.state

    def fail_simulation_run_held_by(
        self,
        simulation_run_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: object,
    ) -> RunWrite:
        assert simulation_run_id == WAITED_RUN_ID
        self.failed_for_calls.append(modal_call_id)
        self.state = _run_state(RunStatus.FAILED, error_message=error_message)
        return RunWrite(changed=True, status=RunStatus.FAILED)


@dataclass
class ModalCallStates:
    """Modal's reported state of every call, and the calls asked about."""

    reported: CallState
    asked: list[str] = field(default_factory=list)

    def __call__(self, modal_call_id: str) -> CallState:
        self.asked.append(modal_call_id)
        return self.reported


def _wait(store: OneRunStore, call_states: ModalCallStates) -> WaitedRun:
    return WaitedRun(
        simulation_run_id=WAITED_RUN_ID,
        writer=store,
        restore=lambda _modal_call_id: LostConnectionCall(),
        call_state=call_states,
        object_id=HOLDING_CALL_ID,
    )


def test_a_failed_probe_of_a_run_that_succeeded_reads_it_without_asking_modal() -> None:
    store = OneRunStore(_run_state(RunStatus.SUCCEEDED))
    call_states = ModalCallStates(CallState.RUNNING)

    outcome = _wait(store, call_states).get(timeout=0)

    assert outcome == WaitedRunSucceeded(store.state)
    assert call_states.asked == []


@pytest.mark.parametrize("reported", [CallState.RUNNING, CallState.UNKNOWN])
def test_a_failed_probe_keeps_waiting_unless_modal_reports_the_call_ended(
    reported: CallState,
) -> None:
    store = OneRunStore(_run_state(RunStatus.RUNNING))
    call_states = ModalCallStates(reported)

    with pytest.raises(TimeoutError):
        _wait(store, call_states).get(timeout=0)

    assert call_states.asked == [HOLDING_CALL_ID]
    assert store.failed_for_calls == []
    assert store.state.status is RunStatus.RUNNING


def test_a_failed_probe_of_an_ended_call_fails_the_run_it_left_unfinished() -> None:
    store = OneRunStore(_run_state(RunStatus.RUNNING))
    call_states = ModalCallStates(CallState.ENDED)

    with pytest.raises(WaitedRunFailedError) as raised:
        _wait(store, call_states).get(timeout=0)

    assert call_states.asked == [HOLDING_CALL_ID]
    assert store.failed_for_calls == [HOLDING_CALL_ID]
    assert raised.value.detail.error_code == "modal_call_ended"
    assert store.state.error_message == ENDED_CALL_MESSAGE


def test_a_failed_probe_of_a_run_moved_to_another_call_follows_it_unasked() -> None:
    later_call_id = "fc-later-worker"
    store = OneRunStore(
        replace(_run_state(RunStatus.RUNNING), modal_call_id=later_call_id)
    )
    call_states = ModalCallStates(CallState.RUNNING)
    waited = _wait(store, call_states)

    with pytest.raises(TimeoutError):
        waited.get(timeout=0)

    assert call_states.asked == []
    assert store.failed_for_calls == []
    assert waited.object_id == later_call_id


def _reading_run_first(store: OneRunStore, restored: list[str]) -> WaitedRun:
    """Return a wait that reads the run before probing a holder that may go on."""

    def restore(modal_call_id: str) -> LostConnectionCall:
        restored.append(modal_call_id)
        return LostConnectionCall()

    return WaitedRun(
        simulation_run_id=WAITED_RUN_ID,
        writer=store,
        restore=restore,
        call_state=ModalCallStates(CallState.RUNNING),
        object_id=HOLDING_CALL_ID,
        read_run_first=True,
    )


def test_reading_first_follows_a_run_moved_to_another_call_without_a_probe() -> None:
    later_call_id = "fc-later-household-function"
    store = OneRunStore(
        replace(_run_state(RunStatus.RUNNING), modal_call_id=later_call_id)
    )
    restored: list[str] = []
    waited = _reading_run_first(store, restored)

    with pytest.raises(TimeoutError):
        waited.get(timeout=0)

    assert restored == []
    assert waited.object_id == later_call_id


def test_reading_first_finds_a_run_left_unstarted_without_a_probe() -> None:
    store = OneRunStore(replace(_run_state(RunStatus.PENDING), modal_call_id=None))
    restored: list[str] = []

    outcome = _reading_run_first(store, restored).get(timeout=0)

    assert outcome == WaitedRunUnstarted()
    assert restored == []
