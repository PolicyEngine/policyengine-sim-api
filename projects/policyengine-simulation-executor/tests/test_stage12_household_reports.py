"""A household function records the household report run it is given.

It claims the report run under its Modal call ID, then each of the report
run's simulation runs, the baseline's and then the reform's: it calculates
each, records each run's household document, and then marks the report run
succeeded, or failed when a side's calculation failed. A report run another
call holds, or one already finished, is not calculated again, and a side a
restarted call already finished keeps its outcome. Modal pre-empts a call
with SIGINT, which a synchronous function receives as ``KeyboardInterrupt``,
and runs it again under the same call ID, so a pre-emption records nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from policyengine_simulation_contract.household_execution import (
    HouseholdCalculationError,
    HouseholdExecutionFailure,
    HouseholdExecutionRequest,
    HouseholdExecutionResult,
    HouseholdWorkerIdentity,
)
from policyengine_simulation_contract.household_report_execution import (
    HouseholdReportRunCalculation,
)
from policyengine_simulation_executor.stage12_runtime import household_report_recording
from policyengine_stage12_persistence import RunStatus, RunWrite, SimulationRunState
from pydantic import ValidationError

REPORT_RUN_ID = UUID("00000000-0000-4000-8000-00000000a001")
BASELINE_RUN_ID = UUID("00000000-0000-4000-8000-0000000000b1")
REFORM_RUN_ID = UUID("00000000-0000-4000-8000-0000000000b2")
CALL_ID = "fc-household-report"
WORKER = HouseholdWorkerIdentity(
    application_name="policyengine-simulation-v2-py6-17-1",
    function_name="calculate_household_us",
)
BUNDLE = {
    "policyengine_version": "6.17.1",
    "core_package_version": "3.33.0",
    "country_package_name": "policyengine-us",
    "country_package_version": "1.764.6",
    "bundle_manifest_sha256": "a" * 64,
}
FAILURE_MESSAGE = "Unrecognized calculate input(s): x"
CHANGED = {
    "running": RunWrite(changed=True, status=RunStatus.RUNNING),
    "succeeded": RunWrite(changed=True, status=RunStatus.SUCCEEDED),
    "failed": RunWrite(changed=True, status=RunStatus.FAILED),
}


def _request(calculation_id: UUID) -> dict[str, Any]:
    return {
        "calculation_id": str(calculation_id),
        "country": "us",
        "year": "2026",
        "household": {"people": {"you": {"age": {"2026": 40}}}},
        "policy": {"parameters": {}},
        "bundle": BUNDLE,
    }


def _calculation(*, with_reform: bool = True) -> HouseholdReportRunCalculation:
    return HouseholdReportRunCalculation.model_validate(
        {
            "baseline": _request(BASELINE_RUN_ID),
            "reform": _request(REFORM_RUN_ID) if with_reform else None,
        }
    )


def _succeeded(request: HouseholdExecutionRequest) -> HouseholdExecutionResult:
    return HouseholdExecutionResult.model_validate(
        {
            "calculation_id": str(request.calculation_id),
            "output": {
                "country": "us",
                "bundle": BUNDLE,
                "household": {"people": {"you": {"age": {"2026": 40}}}},
            },
            "worker": WORKER.model_dump(),
        }
    )


def _failed(request: HouseholdExecutionRequest) -> HouseholdExecutionFailure:
    return HouseholdExecutionFailure(
        calculation_id=request.calculation_id,
        error=HouseholdCalculationError(code="invalid_inputs", message=FAILURE_MESSAGE),
        worker=WORKER,
    )


def _state(
    status: RunStatus, *, error_message: str | None = None
) -> SimulationRunState:
    return SimulationRunState(
        status=status,
        year=2026,
        country_package_version="1.764.6",
        policyengine_version="6.17.1",
        dataset_id=None,
        region_id=None,
        output_uri=None,
        output_sha256=None,
        modal_call_id="fc-earlier",
        error_message=error_message,
    )


@dataclass
class FakeHouseholdReportRuns:
    """Records each write; a test sets what a claim returns or a read finds.

    ``simulation_claims`` and ``simulation_states`` are by simulation run ID;
    a run with no entry is claimed, and read back running under this call.
    """

    report_claim: RunWrite = CHANGED["running"]
    claim_error: Exception | None = None
    # The baseline and reform simulation runs the report run names.
    named_runs: tuple[UUID, UUID | None] = (BASELINE_RUN_ID, REFORM_RUN_ID)
    read_error: Exception | None = None
    report_record_result: RunWrite | None = None
    simulation_claims: dict[UUID, RunWrite] = field(default_factory=dict)
    simulation_states: dict[UUID, SimulationRunState] = field(default_factory=dict)
    record_result: RunWrite | None = None
    writes: list[dict[str, Any]] = field(default_factory=list)

    def claim_report_run(self, report_run_id, *, modal_call_id, claimed_at):
        self.writes.append(
            {"write": "claim_report", "run": report_run_id, "call": modal_call_id}
        )
        if self.claim_error is not None:
            raise self.claim_error
        return self.report_claim

    def household_report_simulation_runs(self, report_run_id):
        self.writes.append({"write": "read_runs", "run": report_run_id})
        if self.read_error is not None:
            raise self.read_error
        return self.named_runs

    def claim_simulation_run(self, simulation_run_id, *, modal_call_id, claimed_at):
        self.writes.append(
            {"write": "claim", "run": simulation_run_id, "call": modal_call_id}
        )
        return self.simulation_claims.get(simulation_run_id, CHANGED["running"])

    def get_simulation_run(self, simulation_run_id):
        return self.simulation_states[simulation_run_id]

    def complete_household_simulation_run(
        self, simulation_run_id, *, modal_call_id, household_result, completed_at
    ):
        self.writes.append(
            {
                "write": "complete",
                "run": simulation_run_id,
                "call": modal_call_id,
                "household_result": household_result,
            }
        )
        return self.record_result or CHANGED["succeeded"]

    def fail_household_simulation_run(
        self,
        simulation_run_id,
        *,
        modal_call_id,
        error_message,
        failed_at,
        household_result=None,
    ):
        self.writes.append(
            {
                "write": "fail",
                "run": simulation_run_id,
                "call": modal_call_id,
                "error_message": error_message,
                "household_result": household_result,
            }
        )
        return self.record_result or CHANGED["failed"]

    def fail_simulation_run(
        self, simulation_run_id, *, error_message, failed_at, modal_call_id=None
    ):
        self.writes.append(
            {
                "write": "fail_unfinished",
                "run": simulation_run_id,
                "call": modal_call_id,
                "error_message": error_message,
            }
        )
        return CHANGED["failed"]

    def complete_household_report_run(
        self, report_run_id, *, modal_call_id, completed_at
    ):
        self.writes.append(
            {"write": "complete_report", "run": report_run_id, "call": modal_call_id}
        )
        return self.report_record_result or CHANGED["succeeded"]

    def fail_report_run(
        self, report_run_id, *, modal_call_id, error_message, failed_at
    ):
        self.writes.append(
            {
                "write": "fail_report",
                "run": report_run_id,
                "call": modal_call_id,
                "error_message": error_message,
            }
        )
        return CHANGED["failed"]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [write for write in self.writes if write["write"] == kind]

    @property
    def kinds(self) -> list[str]:
        return [write["write"] for write in self.writes]


class InputCancelledForTest(BaseException):
    """Stands in for Modal's InputCancellation, a BaseException but not an Exception."""


def _run(runs, calculate, *, with_reform: bool = True, modal_call_id=CALL_ID):
    return household_report_recording.run_household_report(
        REPORT_RUN_ID,
        _calculation(with_reform=with_reform).model_dump(mode="json"),
        calculate,
        modal_call_id=modal_call_id,
        writer=runs,
    )


def test_both_sides_are_calculated_and_recorded_then_the_report_run_succeeds() -> None:
    runs = FakeHouseholdReportRuns()
    calculated: list[UUID] = []

    def calculate(request: HouseholdExecutionRequest) -> HouseholdExecutionResult:
        calculated.append(request.calculation_id)
        return _succeeded(request)

    answer = _run(runs, calculate)

    assert calculated == [BASELINE_RUN_ID, REFORM_RUN_ID]
    assert runs.kinds == [
        "claim_report",
        "read_runs",
        "claim",
        "complete",
        "claim",
        "complete",
        "complete_report",
    ]
    assert all(write["call"] == CALL_ID for write in runs.writes if "call" in write)
    completed = runs.of("complete")
    assert [write["run"] for write in completed] == [BASELINE_RUN_ID, REFORM_RUN_ID]
    assert completed[0]["household_result"] == _succeeded(
        _calculation().baseline
    ).model_dump(mode="json")
    assert answer == {
        "report_run_id": str(REPORT_RUN_ID),
        "status": "succeeded",
        "simulation_runs": [
            {
                "side": "baseline",
                "simulation_run_id": str(BASELINE_RUN_ID),
                "status": "succeeded",
            },
            {
                "side": "reform",
                "simulation_run_id": str(REFORM_RUN_ID),
                "status": "succeeded",
            },
        ],
    }


def test_a_report_of_the_baseline_alone_calculates_one_side() -> None:
    runs = FakeHouseholdReportRuns(named_runs=(BASELINE_RUN_ID, None))

    answer = _run(runs, _succeeded, with_reform=False)

    assert runs.kinds == [
        "claim_report",
        "read_runs",
        "claim",
        "complete",
        "complete_report",
    ]
    assert answer["status"] == "succeeded"
    assert len(answer["simulation_runs"]) == 1


def test_a_failed_side_is_recorded_and_fails_the_report_run_after_both_sides() -> None:
    runs = FakeHouseholdReportRuns()

    def calculate(request: HouseholdExecutionRequest):
        if request.calculation_id == BASELINE_RUN_ID:
            return _failed(request)
        return _succeeded(request)

    answer = _run(runs, calculate)

    (failed,) = runs.of("fail")
    assert failed["run"] == BASELINE_RUN_ID
    assert failed["error_message"] == f"invalid_inputs: {FAILURE_MESSAGE}"
    assert failed["household_result"] == _failed(_calculation().baseline).model_dump(
        mode="json"
    )
    # The reform is still calculated, so each run has its own outcome.
    assert [write["run"] for write in runs.of("complete")] == [REFORM_RUN_ID]
    (failed_report,) = runs.of("fail_report")
    assert failed_report["error_message"] == (
        "household_calculation_failed: The baseline simulation run failed: "
        f"invalid_inputs: {FAILURE_MESSAGE}"
    )
    assert "complete_report" not in runs.kinds
    assert answer["status"] == "failed"
    assert [run["status"] for run in answer["simulation_runs"]] == [
        "failed",
        "succeeded",
    ]


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.SUCCEEDED])
def test_a_report_run_another_call_holds_or_that_finished_is_not_calculated(
    status: RunStatus,
) -> None:
    runs = FakeHouseholdReportRuns(report_claim=RunWrite(changed=False, status=status))

    def calculate(request: HouseholdExecutionRequest) -> HouseholdExecutionResult:
        pytest.fail("a report run this call does not hold is not calculated")

    answer = _run(runs, calculate)

    assert runs.kinds == ["claim_report"]
    assert answer == {
        "deduplicated": True,
        "report_run_id": str(REPORT_RUN_ID),
        "status": status.value,
    }


def test_a_restarted_call_keeps_a_side_it_already_recorded() -> None:
    runs = FakeHouseholdReportRuns(
        simulation_claims={
            BASELINE_RUN_ID: RunWrite(changed=False, status=RunStatus.SUCCEEDED)
        },
        simulation_states={BASELINE_RUN_ID: _state(RunStatus.SUCCEEDED)},
    )
    calculated: list[UUID] = []

    def calculate(request: HouseholdExecutionRequest) -> HouseholdExecutionResult:
        calculated.append(request.calculation_id)
        return _succeeded(request)

    answer = _run(runs, calculate)

    assert calculated == [REFORM_RUN_ID]
    assert [write["run"] for write in runs.of("complete")] == [REFORM_RUN_ID]
    assert runs.kinds[-1] == "complete_report"
    assert answer["status"] == "succeeded"


def test_a_side_that_already_failed_fails_the_report_run_with_its_failure() -> None:
    recorded = f"invalid_inputs: {FAILURE_MESSAGE}"
    runs = FakeHouseholdReportRuns(
        simulation_claims={
            REFORM_RUN_ID: RunWrite(changed=False, status=RunStatus.FAILED)
        },
        simulation_states={
            REFORM_RUN_ID: _state(RunStatus.FAILED, error_message=recorded)
        },
    )

    answer = _run(runs, _succeeded)

    assert [write["run"] for write in runs.of("complete")] == [BASELINE_RUN_ID]
    assert runs.of("fail") == []
    (failed_report,) = runs.of("fail_report")
    assert failed_report["error_message"] == (
        "household_calculation_failed: The reform simulation run failed: " + recorded
    )
    assert answer["status"] == "failed"


def test_a_side_another_call_holds_is_not_calculated_and_fails_the_report_run() -> None:
    runs = FakeHouseholdReportRuns(
        simulation_claims={
            REFORM_RUN_ID: RunWrite(changed=False, status=RunStatus.RUNNING)
        },
        simulation_states={REFORM_RUN_ID: _state(RunStatus.RUNNING)},
    )
    calculated: list[UUID] = []

    def calculate(request: HouseholdExecutionRequest) -> HouseholdExecutionResult:
        calculated.append(request.calculation_id)
        return _succeeded(request)

    answer = _run(runs, calculate)

    assert calculated == [BASELINE_RUN_ID]
    (failed_report,) = runs.of("fail_report")
    assert failed_report["error_message"] == (
        "household_simulation_run_unavailable: The reform simulation run is "
        "running under another call."
    )
    assert answer["status"] == "failed"


@pytest.mark.parametrize(
    ("error", "summary"),
    [
        (
            RuntimeError("the model could not load"),
            "household_function_error: The household function raised RuntimeError.",
        ),
        (
            InputCancelledForTest(),
            "household_function_cancelled: Modal cancelled the household "
            "function's call.",
        ),
    ],
    ids=["error", "cancellation"],
)
def test_an_unexpected_error_fails_every_unfinished_run_then_propagates(
    error: BaseException, summary: str
) -> None:
    runs = FakeHouseholdReportRuns()

    def calculate(request: HouseholdExecutionRequest) -> HouseholdExecutionResult:
        raise error

    with pytest.raises(type(error)):
        _run(runs, calculate)

    # The error names only its type: its text may repeat the household.
    (failed_side,) = runs.of("fail")
    assert (failed_side["run"], failed_side["call"]) == (BASELINE_RUN_ID, CALL_ID)
    assert failed_side["error_message"] == summary
    assert failed_side["household_result"] is None
    (unfinished,) = runs.of("fail_unfinished")
    assert unfinished["run"] == REFORM_RUN_ID
    assert unfinished["error_message"] == summary
    (failed_report,) = runs.of("fail_report")
    assert (failed_report["call"], failed_report["error_message"]) == (CALL_ID, summary)


def test_a_preemption_records_nothing_and_propagates() -> None:
    runs = FakeHouseholdReportRuns()

    def calculate(request: HouseholdExecutionRequest) -> HouseholdExecutionResult:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run(runs, calculate)

    assert runs.kinds == ["claim_report", "read_runs", "claim"]


def test_a_report_run_naming_other_simulation_runs_fails_with_its_own_runs() -> None:
    own_runs = (
        UUID("00000000-0000-4000-8000-0000000000d1"),
        UUID("00000000-0000-4000-8000-0000000000d2"),
    )
    runs = FakeHouseholdReportRuns(named_runs=own_runs)

    def calculate(request: HouseholdExecutionRequest) -> HouseholdExecutionResult:
        pytest.fail("a report run naming other simulation runs is not calculated")

    with pytest.raises(ValueError, match="names other simulation runs"):
        _run(runs, calculate)

    summary = "household_function_error: The household function raised ValueError."
    assert runs.kinds == [
        "claim_report",
        "read_runs",
        "fail_unfinished",
        "fail_unfinished",
        "fail_report",
    ]
    # The report run's own runs are failed, never the payload's.
    assert [write["run"] for write in runs.of("fail_unfinished")] == list(own_runs)
    assert {write["error_message"] for write in runs.of("fail_unfinished")} == {summary}
    assert runs.of("fail_report")[0]["error_message"] == summary


def test_a_report_run_whose_runs_cannot_be_read_is_failed_alone() -> None:
    runs = FakeHouseholdReportRuns(read_error=ConnectionError("connection lost"))

    with pytest.raises(ConnectionError):
        _run(runs, _succeeded)

    assert runs.kinds == ["claim_report", "read_runs", "fail_report"]


def test_a_report_run_another_writer_finished_is_answered_as_it_stands(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runs = FakeHouseholdReportRuns(
        report_record_result=RunWrite(changed=False, status=RunStatus.FAILED)
    )

    answer = _run(runs, _succeeded)

    assert answer["status"] == "failed"
    assert f"Report run {REPORT_RUN_ID} was failed before" in caplog.text


def test_a_call_without_a_modal_call_id_writes_nothing() -> None:
    runs = FakeHouseholdReportRuns()

    with pytest.raises(RuntimeError, match="call identifier is unavailable"):
        _run(runs, _succeeded, modal_call_id=None)

    assert runs.writes == []


def test_a_side_whose_record_changes_nothing_fails_the_report_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Another writer finished the run this call held before its record.
    runs = FakeHouseholdReportRuns(
        named_runs=(BASELINE_RUN_ID, None),
        record_result=RunWrite(changed=False, status=RunStatus.FAILED),
    )

    answer = _run(runs, _succeeded, with_reform=False)

    assert answer["simulation_runs"][0]["status"] == "failed"
    assert f"Simulation run {BASELINE_RUN_ID} was failed before" in caplog.text
    assert "complete_report" not in runs.kinds
    (failed_report,) = runs.of("fail_report")
    assert failed_report["error_message"] == (
        "household_simulation_run_unavailable: The baseline simulation run was "
        "failed before its calculation was recorded."
    )
    assert answer["status"] == "failed"


def test_a_side_found_failed_with_no_recorded_failure_fails_the_report_run() -> None:
    runs = FakeHouseholdReportRuns(
        simulation_claims={
            REFORM_RUN_ID: RunWrite(changed=False, status=RunStatus.FAILED)
        },
        simulation_states={REFORM_RUN_ID: _state(RunStatus.FAILED)},
    )

    answer = _run(runs, _succeeded)

    (failed_report,) = runs.of("fail_report")
    assert failed_report["error_message"] == (
        "household_calculation_failed: The reform simulation run failed: "
        "no failure was recorded"
    )
    assert answer["status"] == "failed"


def test_a_payload_the_contract_refuses_fails_the_report_run_it_claimed() -> None:
    runs = FakeHouseholdReportRuns()

    def calculate(request: HouseholdExecutionRequest) -> HouseholdExecutionResult:
        pytest.fail("a refused payload is not calculated")

    with pytest.raises(ValidationError):
        household_report_recording.run_household_report(
            REPORT_RUN_ID,
            {"baseline": {"calculation_id": "not a request"}},
            calculate,
            modal_call_id=CALL_ID,
            writer=runs,
        )

    summary = "household_function_error: The household function raised ValidationError."
    # The report run's own simulation runs fail with it, read from the database.
    assert runs.kinds == [
        "claim_report",
        "read_runs",
        "fail_unfinished",
        "fail_unfinished",
        "fail_report",
    ]
    assert [write["run"] for write in runs.of("fail_unfinished")] == [
        BASELINE_RUN_ID,
        REFORM_RUN_ID,
    ]
    assert runs.of("fail_report")[0]["error_message"] == summary


def test_a_claim_that_raises_fails_the_report_run_it_may_hold_then_propagates() -> None:
    # The claim's commit may have applied before its connection failed.
    runs = FakeHouseholdReportRuns(claim_error=ConnectionError("connection lost"))

    with pytest.raises(ConnectionError):
        _run(runs, _succeeded)

    assert runs.kinds == ["claim_report", "fail_report"]
    assert runs.of("fail_report")[0]["call"] == CALL_ID
