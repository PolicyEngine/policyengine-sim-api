"""Recording a household function's outcome on the runs its call computes."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from household_support import (
    BUNDLE,
    DEPRECATED_INPUT_WARNING,
    SPM_CONFIG,
    calculated_household,
    failed_outcome,
    spm_receipt,
    succeeded_outcome,
)
from policyengine_simulation_contract.call_reports import (
    CallEndedReport,
    CallReportStatus,
)
from policyengine_simulation_entry.household_recording import (
    HouseholdResultRecorder,
    policy_key_for_run,
)
from policyengine_simulation_entry.modal_calls import CallSucceeded
from policyengine_simulation_entry.run_recording import (
    CalculationFailedError,
    ResultRecorders,
    RunRecorder,
)
from policyengine_simulation_entry.simulation_database import SimulationDatabase
from policyengine_simulation_persistence import (
    RunErrorCode,
    RunStatus,
    SimulationKind,
)
from run_fakes import FakeInspector, FakeRunRecord, FakeRunTransitions, ManualClock


class OutputCapture:
    """Record each household output written, by run."""

    def __init__(self) -> None:
        """Start with nothing written."""
        self.written: dict = {}

    def __call__(self, connection, run, output) -> None:
        """Keep the output under its run's ID."""
        self.written[run.id] = (connection, output)


def household_run(run_id) -> FakeRunRecord:
    """Return a running household run record."""
    return FakeRunRecord(
        id=run_id,
        simulation_kind=SimulationKind.HOUSEHOLD,
        modal_call_id="fc-household",
        check_count=0,
    )


def test_each_run_gets_its_own_household_with_the_calculations_notes() -> None:
    capture = OutputCapture()
    recorder = HouseholdResultRecorder(write_output=capture)
    baseline_id, reform_id = uuid4(), uuid4()
    connection = object()

    result = recorder.validate(succeeded_outcome([baseline_id, reform_id]))
    recorder.write(
        connection, (household_run(reform_id), household_run(baseline_id)), result
    )

    for run_id, net_income in ((baseline_id, 1000.0), (reform_id, 2000.0)):
        written_connection, output = capture.written[run_id]
        assert written_connection is connection
        assert output.schema_version == 1
        assert output.household == calculated_household(net_income)
        assert list(output.household) == ["households", "people"]
        assert output.warnings == (DEPRECATED_INPUT_WARNING,)
        assert output.spm_config == SPM_CONFIG
        assert output.spm_provenance == spm_receipt()
        assert output.bundle == BUNDLE


def test_a_rejected_calculation_fails_with_the_household_error_as_detail() -> None:
    recorder = HouseholdResultRecorder(write_output=OutputCapture())

    with pytest.raises(CalculationFailedError) as raised:
        recorder.validate(failed_outcome())

    error = raised.value.error
    assert error.code is RunErrorCode.HOUSEHOLD_CALCULATION_FAILED
    assert error.message == "The household calculation failed: invalid_inputs."
    assert "employment_incme" not in error.message
    assert error.detail == failed_outcome()["error"]


def test_an_output_that_is_no_household_outcome_is_invalid() -> None:
    recorder = HouseholdResultRecorder(write_output=OutputCapture())

    with pytest.raises(ValueError, match="validation error"):
        recorder.validate({"status": "succeeded", "output": {}})


def test_nothing_is_written_when_the_output_misses_one_of_the_runs() -> None:
    capture = OutputCapture()
    recorder = HouseholdResultRecorder(write_output=capture)
    covered, missing = uuid4(), uuid4()
    result = recorder.validate(succeeded_outcome([covered]))

    with pytest.raises(ValueError, match="no result for run"):
        recorder.write(
            object(), (household_run(covered), household_run(missing)), result
        )

    assert capture.written == {}


def test_a_policy_key_names_its_run() -> None:
    run_id = uuid4()

    assert policy_key_for_run(run_id) == f"run-{run_id.hex}"


def _recorder(capture: OutputCapture):
    clock = ManualClock()
    transitions = FakeRunTransitions(clock)
    inspector = FakeInspector()
    recorder = RunRecorder(
        transitions=transitions,
        runs=transitions,
        inspector=inspector,
        recorders=ResultRecorders(
            simulations={
                SimulationKind.HOUSEHOLD: HouseholdResultRecorder(write_output=capture)
            }
        ),
        clock=clock,
    )
    return recorder, transitions, inspector


def test_a_reported_rejection_fails_both_runs_of_a_household_report() -> None:
    recorder, transitions, inspector = _recorder(OutputCapture())
    run_ids = transitions.running_call("fc-household", "household", "household")
    inspector.outcomes["fc-household"] = CallSucceeded(failed_outcome())

    status = asyncio.run(
        recorder.record_reported_call(
            CallEndedReport(
                modal_call_id="fc-household", simulation_run_ids=tuple(run_ids)
            )
        )
    )

    assert status is CallReportStatus.RECORDED
    for run_id in run_ids:
        run = transitions.runs[run_id]
        assert run.status is RunStatus.FAILED
        assert run.error.code is RunErrorCode.HOUSEHOLD_CALCULATION_FAILED
        assert run.error.detail == failed_outcome()["error"]


def test_a_reported_calculation_succeeds_both_runs_with_their_households() -> None:
    capture = OutputCapture()
    recorder, transitions, inspector = _recorder(capture)
    run_ids = transitions.running_call("fc-household", "household", "household")
    inspector.outcomes["fc-household"] = CallSucceeded(succeeded_outcome(run_ids))

    status = asyncio.run(
        recorder.record_reported_call(
            CallEndedReport(
                modal_call_id="fc-household", simulation_run_ids=tuple(run_ids)
            )
        )
    )

    assert status is CallReportStatus.RECORDED
    assert {transitions.runs[run_id].status for run_id in run_ids} == {
        RunStatus.SUCCEEDED
    }
    assert set(capture.written) == set(run_ids)


def test_the_database_recorder_records_household_calls() -> None:
    database = SimulationDatabase.from_url(
        "postgresql://simulation_runtime:secret@db.example:5432/postgres"
    )
    try:
        recorders = database.call_recorder().recorders
    finally:
        database.dispose()

    assert isinstance(
        recorders.simulations[SimulationKind.HOUSEHOLD], HouseholdResultRecorder
    )
