"""Recording a call's outcome when its Modal function reports that it ended."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from policyengine_simulation_contract.call_reports import (
    CallEndedReport,
    CallReportStatus,
)
from policyengine_simulation_entry.modal_calls import (
    CallFailed,
    CallInspectionError,
    CallSucceeded,
)
from policyengine_simulation_entry.run_recording import ResultRecorders, RunRecorder
from policyengine_simulation_persistence import (
    ReportKind,
    RunError,
    RunErrorCode,
    RunStatus,
    SimulationKind,
)
from run_fakes import FakeInspector, FakeRunTransitions, ManualClock
from test_run_recording import RecordingRecorder

WORKER_TIMEOUT = RunError(RunErrorCode.WORKER_TIMEOUT, "The Modal call timed out.")


def _recorder(recorders=None):
    clock = ManualClock()
    transitions = FakeRunTransitions(clock)
    inspector = FakeInspector()
    recorder = RunRecorder(
        transitions=transitions,
        runs=transitions,
        inspector=inspector,
        recorders=recorders or ResultRecorders(),
        clock=clock,
    )
    return recorder, transitions, inspector


def _economy_recorder():
    budget = RecordingRecorder()
    recorder, transitions, inspector = _recorder(
        ResultRecorders(simulations={SimulationKind.ECONOMY: budget})
    )
    return recorder, transitions, inspector, budget


def _report(recorder, **fields) -> CallReportStatus:
    return asyncio.run(recorder.record_reported_call(CallEndedReport(**fields)))


def test_a_finished_call_is_recorded_when_it_reports() -> None:
    recorder, transitions, inspector, budget = _economy_recorder()
    (run_id,) = transitions.running_call("fc-done")
    inspector.outcomes["fc-done"] = CallSucceeded({"budgetary_impact": 2.5})

    status = _report(recorder, modal_call_id="fc-done", simulation_run_ids=(run_id,))

    assert status is CallReportStatus.RECORDED
    assert transitions.runs[run_id].status is RunStatus.SUCCEEDED
    assert len(budget.writes) == 1


def test_a_repeated_report_changes_nothing() -> None:
    recorder, transitions, inspector, budget = _economy_recorder()
    (run_id,) = transitions.running_call("fc-done")
    inspector.outcomes["fc-done"] = CallSucceeded({"budgetary_impact": 2.5})
    _report(recorder, modal_call_id="fc-done", simulation_run_ids=(run_id,))

    status = _report(recorder, modal_call_id="fc-done", simulation_run_ids=(run_id,))

    assert status is CallReportStatus.ALREADY_RECORDED
    assert len(budget.writes) == 1
    assert inspector.inspected == ["fc-done"]


def test_a_household_pair_is_recorded_from_one_report() -> None:
    household = RecordingRecorder()
    recorder, transitions, inspector = _recorder(
        ResultRecorders(simulations={SimulationKind.HOUSEHOLD: household})
    )
    pair = transitions.running_call("fc-pair", "household", "household")
    inspector.outcomes["fc-pair"] = CallSucceeded({"budgetary_impact": 0})

    status = _report(recorder, modal_call_id="fc-pair", simulation_run_ids=tuple(pair))

    assert status is CallReportStatus.RECORDED
    assert {transitions.runs[run_id].status for run_id in pair} == {RunStatus.SUCCEEDED}
    assert len(household.writes) == 1


def test_a_failed_call_is_recorded_as_failed() -> None:
    recorder, transitions, inspector = _recorder()
    (run_id,) = transitions.running_call("fc-failed")
    inspector.outcomes["fc-failed"] = CallFailed(WORKER_TIMEOUT)

    status = _report(recorder, modal_call_id="fc-failed", simulation_run_ids=(run_id,))

    assert status is CallReportStatus.RECORDED
    assert transitions.runs[run_id].status is RunStatus.FAILED
    assert transitions.runs[run_id].error.code is RunErrorCode.WORKER_TIMEOUT


def test_a_call_modal_still_runs_is_left_for_another_report() -> None:
    recorder, transitions, _, _ = _economy_recorder()
    (run_id,) = transitions.running_call("fc-running")
    next_check_at = transitions.runs[run_id].next_check_at

    status = _report(recorder, modal_call_id="fc-running", simulation_run_ids=(run_id,))

    assert status is CallReportStatus.STILL_RUNNING
    assert transitions.runs[run_id].status is RunStatus.RUNNING
    assert transitions.runs[run_id].next_check_at == next_check_at


def test_a_call_whose_id_is_not_stored_yet_is_left_for_another_report() -> None:
    """A fast household call can report before its dispatcher stores its ID."""
    recorder, transitions, inspector, _ = _economy_recorder()
    run_id = transitions.add_run(status=RunStatus.DISPATCHING)

    status = _report(recorder, modal_call_id="fc-fast", simulation_run_ids=(run_id,))

    assert status is CallReportStatus.NOT_DISPATCHED_YET
    assert inspector.inspected == []
    assert transitions.runs[run_id].status is RunStatus.DISPATCHING


def test_a_report_naming_an_unknown_run_is_refused() -> None:
    recorder, transitions, inspector, _ = _economy_recorder()
    (run_id,) = transitions.running_call("fc-done")

    status = _report(
        recorder, modal_call_id="fc-done", simulation_run_ids=(run_id, uuid4())
    )

    assert status is CallReportStatus.UNKNOWN_RUNS
    assert inspector.inspected == []


@pytest.mark.parametrize(
    "run_state",
    [
        {"status": RunStatus.RUNNING, "modal_call_id": "fc-other"},
        {"status": RunStatus.SUCCEEDED, "modal_call_id": "fc-other"},
        # Failed as abandoned before the call's ID was stored.
        {"status": RunStatus.FAILED, "modal_call_id": None},
    ],
    ids=["running-another-call", "finished-another-call", "failed-without-call"],
)
def test_a_report_for_runs_that_do_not_hold_its_call_is_refused(run_state) -> None:
    recorder, transitions, inspector, _ = _economy_recorder()
    run_id = transitions.add_run(**run_state)

    status = _report(recorder, modal_call_id="fc-late", simulation_run_ids=(run_id,))

    assert status is CallReportStatus.OTHER_CALL
    assert inspector.inspected == []
    assert transitions.runs[run_id].status is run_state["status"]


def test_an_unreachable_modal_is_left_for_another_report() -> None:
    recorder, transitions, inspector, _ = _economy_recorder()
    (run_id,) = transitions.running_call("fc-done")
    inspector.outcomes["fc-done"] = CallInspectionError("ConnectionError")

    status = _report(recorder, modal_call_id="fc-done", simulation_run_ids=(run_id,))

    assert status is CallReportStatus.MODAL_UNREACHABLE
    assert transitions.runs[run_id].status is RunStatus.RUNNING


def test_a_kind_without_a_recorder_is_left_for_a_revision_with_one() -> None:
    recorder, transitions, inspector = _recorder()
    (run_id,) = transitions.running_call("fc-done")
    inspector.outcomes["fc-done"] = CallSucceeded({"budgetary_impact": 1})

    status = _report(recorder, modal_call_id="fc-done", simulation_run_ids=(run_id,))

    assert status is CallReportStatus.NO_RESULT_RECORDER
    assert transitions.runs[run_id].status is RunStatus.RUNNING


def test_a_failed_read_is_left_for_another_report() -> None:
    recorder, transitions, inspector, _ = _economy_recorder()
    (run_id,) = transitions.running_call("fc-done")

    def unreachable_database(_run_id):
        msg = "connection lost"
        raise ConnectionError(msg)

    transitions.get_simulation_run = unreachable_database  # type: ignore[method-assign]

    status = _report(recorder, modal_call_id="fc-done", simulation_run_ids=(run_id,))

    assert status is CallReportStatus.NOT_WRITTEN
    assert inspector.inspected == []


@pytest.mark.parametrize(
    ("outcome", "write"),
    [
        (CallSucceeded({"budgetary_impact": 2.5}), "complete_simulation_call"),
        (CallFailed(WORKER_TIMEOUT), "fail_simulation_call"),
    ],
    ids=["result", "failure"],
)
def test_a_failed_write_is_left_for_another_report(outcome, write) -> None:
    recorder, transitions, inspector, _ = _economy_recorder()
    (run_id,) = transitions.running_call("fc-done")
    inspector.outcomes["fc-done"] = outcome

    def unreachable_database(*_arguments):
        msg = "connection lost"
        raise ConnectionError(msg)

    setattr(transitions, write, unreachable_database)

    status = _report(recorder, modal_call_id="fc-done", simulation_run_ids=(run_id,))

    assert status is CallReportStatus.NOT_WRITTEN
    assert transitions.runs[run_id].status is RunStatus.RUNNING


def test_a_signal_to_this_process_is_not_swallowed() -> None:
    recorder, transitions, inspector, _ = _economy_recorder()
    (run_id,) = transitions.running_call("fc-done")
    inspector.outcomes["fc-done"] = CallFailed(WORKER_TIMEOUT)

    def interrupted(*_arguments):
        raise KeyboardInterrupt

    transitions.fail_simulation_call = interrupted  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt):
        _report(recorder, modal_call_id="fc-done", simulation_run_ids=(run_id,))


def test_a_comparison_call_is_recorded_when_it_reports() -> None:
    comparisons = RecordingRecorder()
    recorder, transitions, inspector = _recorder(
        ResultRecorders(comparisons={ReportKind.ECONOMY: comparisons})
    )
    report_run_id = transitions.add_comparison(
        status=RunStatus.RUNNING, modal_call_id="fc-comparison"
    )
    inspector.outcomes["fc-comparison"] = CallSucceeded({"budgetary_impact": 3})

    status = _report(
        recorder, modal_call_id="fc-comparison", report_run_id=report_run_id
    )

    assert status is CallReportStatus.RECORDED
    assert transitions.comparisons[report_run_id].status is RunStatus.SUCCEEDED
    assert [record.id for _, record, _ in comparisons.writes] == [report_run_id]


def test_a_comparison_report_for_an_unknown_report_run_is_refused() -> None:
    recorder, _, inspector, _ = _economy_recorder()

    status = _report(recorder, modal_call_id="fc-comparison", report_run_id=uuid4())

    assert status is CallReportStatus.UNKNOWN_RUNS
    assert inspector.inspected == []


def test_a_comparison_whose_call_id_is_not_stored_yet_waits() -> None:
    recorder, transitions, _, _ = _economy_recorder()
    report_run_id = transitions.add_comparison(status=RunStatus.DISPATCHING)

    status = _report(
        recorder, modal_call_id="fc-comparison", report_run_id=report_run_id
    )

    assert status is CallReportStatus.NOT_DISPATCHED_YET
