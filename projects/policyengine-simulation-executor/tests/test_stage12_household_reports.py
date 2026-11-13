"""A household function records the household report run it is given.

It claims the report run under its Modal call ID, then each of the report
run's simulation runs, the baseline's and then the reform's: it calculates
each, records each run's household document, and then marks the report run
succeeded only if every side's run did, else failed. A report run another
call holds, or one already finished, is not calculated again, and a side a
restarted call already finished keeps its outcome. A side another call holds,
as another household report run may share it, is waited for after the sides
this call can claim; a run named on both sides is calculated once, and a
failing call fails only the runs it holds. Modal pre-empts a call
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
from policyengine_simulation_executor.stage12_runtime.run_sweep import (
    ENDED_CALL_MESSAGE,
    CallState,
)
from policyengine_simulation_executor.stage12_runtime.shared_simulation_runs import (
    HeldRunWait,
)
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
    # Simulation runs whose claim raises, by run ID.
    simulation_claim_errors: dict[UUID, Exception] = field(default_factory=dict)
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
        if simulation_run_id in self.simulation_claim_errors:
            raise self.simulation_claim_errors[simulation_run_id]
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

    def fail_simulation_run_held_by(
        self, simulation_run_id, *, modal_call_id, error_message, failed_at
    ):
        self.writes.append(
            {
                "write": "fail_held_by",
                "run": simulation_run_id,
                "call": modal_call_id,
                "error_message": error_message,
            }
        )
        state = self.simulation_states[simulation_run_id]
        if state.modal_call_id != modal_call_id or state.status not in {
            RunStatus.PENDING,
            RunStatus.RUNNING,
        }:
            return RunWrite(changed=False, status=state.status)
        self.simulation_states[simulation_run_id] = _state(
            RunStatus.FAILED, error_message=error_message
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


def _run(  # noqa: PLR0913  # the stores, then the call's keyword-only seams
    runs,
    calculate,
    *,
    with_reform: bool = True,
    modal_call_id=CALL_ID,
    wait=None,
    calculation=None,
):
    calculation = calculation or _calculation(with_reform=with_reform)
    return household_report_recording.run_household_report(
        REPORT_RUN_ID,
        calculation.model_dump(mode="json"),
        calculate,
        modal_call_id=modal_call_id,
        writer=runs,
        wait=wait,
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


def test_a_side_another_call_holds_past_the_wait_fails_the_report_unwritten() -> None:
    runs = FakeHouseholdReportRuns(
        simulation_claims={
            REFORM_RUN_ID: RunWrite(changed=False, status=RunStatus.RUNNING)
        },
        simulation_states={REFORM_RUN_ID: _state(RunStatus.RUNNING)},
    )
    holder = _Holder(runs, REFORM_RUN_ID, ends_as="never")
    calculated: list[UUID] = []

    def calculate(request: HouseholdExecutionRequest) -> HouseholdExecutionResult:
        calculated.append(request.calculation_id)
        return _succeeded(request)

    answer = _run(runs, calculate, wait=_wait(holder, timeout_seconds=5))

    assert calculated == [BASELINE_RUN_ID]
    (failed_report,) = runs.of("fail_report")
    assert failed_report["error_message"] == (
        "household_simulation_run_unavailable: The reform simulation run is "
        "still running under another call after 5 seconds."
    )
    assert answer["status"] == "failed"
    # The run is its holder's: nothing is written to it or its call.
    assert [write["run"] for write in runs.writes if "run" in write].count(
        REFORM_RUN_ID
    ) == 1
    assert holder.cancelled == 0


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

    # The error names only its type: its text may repeat the household. Only
    # the run this call holds is failed, and only while it does: the reform,
    # never claimed, may be another report run's too.
    assert runs.of("fail") == []
    unfinished = runs.of("fail_unfinished")
    assert [write["run"] for write in unfinished] == [BASELINE_RUN_ID]
    assert {write["error_message"] for write in unfinished} == {summary}
    assert {write["call"] for write in unfinished} == {CALL_ID}
    (failed_report,) = runs.of("fail_report")
    assert (failed_report["call"], failed_report["error_message"]) == (CALL_ID, summary)


def test_a_side_whose_claim_raises_is_failed_only_while_this_call_holds_it() -> None:
    # The claim may have applied, or rolled back leaving the run pending with
    # no call: the failure applies only in the first case.
    runs = FakeHouseholdReportRuns(
        simulation_claim_errors={BASELINE_RUN_ID: ConnectionError("lost")}
    )

    with pytest.raises(ConnectionError):
        _run(runs, _succeeded)

    assert runs.kinds == [
        "claim_report",
        "read_runs",
        "claim",
        "fail_unfinished",
        "fail_report",
    ]
    assert [write["run"] for write in runs.of("fail_unfinished")] == [BASELINE_RUN_ID]
    assert {write["call"] for write in runs.of("fail_unfinished")} == {CALL_ID}


def _reform_raises(request: HouseholdExecutionRequest) -> HouseholdExecutionResult:
    if request.calculation_id == REFORM_RUN_ID:
        msg = "the model could not load"
        raise RuntimeError(msg)
    return _succeeded(request)


@pytest.mark.parametrize(
    "baseline",
    ["recorded", "held-by-another-call"],
)
def test_an_error_leaves_a_side_this_call_finished_or_does_not_hold_alone(
    baseline: str,
) -> None:
    # Only the reform, which this call holds, is left to fail.
    runs = (
        FakeHouseholdReportRuns()
        if baseline == "recorded"
        else FakeHouseholdReportRuns(
            simulation_claims={
                BASELINE_RUN_ID: RunWrite(changed=False, status=RunStatus.RUNNING)
            },
            simulation_states={BASELINE_RUN_ID: _state(RunStatus.RUNNING)},
        )
    )

    with pytest.raises(RuntimeError):
        _run(runs, _reform_raises)

    assert [write["run"] for write in runs.of("fail_unfinished")] == [REFORM_RUN_ID]
    assert len(runs.of("fail_report")) == 1


@pytest.mark.parametrize(
    ("named_runs", "with_reform"),
    [((BASELINE_RUN_ID, None), True), ((BASELINE_RUN_ID, REFORM_RUN_ID), False)],
    ids=["payload-adds-a-reform", "payload-leaves-out-the-reform"],
)
def test_a_payload_whose_reform_differs_from_the_report_runs_is_refused(
    named_runs: tuple[UUID, UUID | None], *, with_reform: bool
) -> None:
    runs = FakeHouseholdReportRuns(named_runs=named_runs)

    def calculate(request: HouseholdExecutionRequest) -> HouseholdExecutionResult:
        pytest.fail("a payload naming other runs is not calculated")

    with pytest.raises(ValueError, match="names other simulation runs"):
        _run(runs, calculate, with_reform=with_reform)

    assert [write["run"] for write in runs.of("fail_unfinished")] == [
        run_id for run_id in named_runs if run_id is not None
    ]
    assert len(runs.of("fail_report")) == 1


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


class _Holder:
    """The call another household report run's function holds a run under.

    Each probe raises TimeoutError until ``probes_before_ending`` have
    passed; then it ends as ``ends_as`` says: ``succeeds`` records the run
    succeeded, ``fails`` records its failure and raises, ``stops`` raises
    with the run still running under it, and ``never`` doesn't end.
    """

    def __init__(
        self, runs, run_id, *, ends_as: str, probes_before_ending: int = 1
    ) -> None:
        self.object_id = "fc-earlier"
        self.runs = runs
        self.run_id = run_id
        self.ends_as = ends_as
        self.probes_before_ending = probes_before_ending
        self.probes = 0
        self.cancelled = 0

    def get(self, *, timeout=None):
        self.probes += 1
        if self.ends_as == "never" or self.probes <= self.probes_before_ending:
            msg = "the holder call is still running"
            raise TimeoutError(msg)
        if self.ends_as == "succeeds":
            self.runs.simulation_states[self.run_id] = _state(RunStatus.SUCCEEDED)
            return {}
        if self.ends_as == "fails":
            self.runs.simulation_states[self.run_id] = _state(
                RunStatus.FAILED, error_message=FAILURE_MESSAGE
            )
        msg = "the holder call ended"
        raise RuntimeError(msg)

    def cancel(self) -> None:
        self.cancelled += 1


class _Clock:
    """A monotonic clock that a wait's sleeps move on."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _wait(holder, *, timeout_seconds=60, call_state=None) -> HeldRunWait:
    clock = _Clock()
    return HeldRunWait(
        restore=lambda _modal_call_id: holder,
        call_state=call_state or (lambda _modal_call_id: CallState.ENDED),
        timeout_seconds=timeout_seconds,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


def _held_baseline(ends_as: str, **holder_options):
    runs = FakeHouseholdReportRuns(
        simulation_claims={
            BASELINE_RUN_ID: RunWrite(changed=False, status=RunStatus.RUNNING)
        },
        simulation_states={BASELINE_RUN_ID: _state(RunStatus.RUNNING)},
    )
    return runs, _Holder(runs, BASELINE_RUN_ID, ends_as=ends_as, **holder_options)


def _recording_calculate(calculated: list[UUID]):
    def calculate(request: HouseholdExecutionRequest) -> HouseholdExecutionResult:
        calculated.append(request.calculation_id)
        return _succeeded(request)

    return calculate


def test_a_held_side_is_waited_for_after_the_others_and_used_once_it_succeeds() -> None:
    runs, holder = _held_baseline("succeeds")
    calculated: list[UUID] = []

    answer = _run(runs, _recording_calculate(calculated), wait=_wait(holder))

    # The reform, which this call could claim, is calculated first; the
    # baseline, which another call holds, is waited for and never written.
    assert calculated == [REFORM_RUN_ID]
    assert runs.kinds == [
        "claim_report",
        "read_runs",
        "claim",
        "claim",
        "complete",
        "complete_report",
    ]
    assert answer["status"] == "succeeded"
    assert [side["status"] for side in answer["simulation_runs"]] == [
        "succeeded",
        "succeeded",
    ]
    assert holder.cancelled == 0


def test_a_held_side_that_fails_fails_the_report_run_with_its_failure() -> None:
    runs, holder = _held_baseline("fails")

    answer = _run(runs, _succeeded, wait=_wait(holder))

    (failed_report,) = runs.of("fail_report")
    assert failed_report["error_message"] == (
        "household_calculation_failed: The baseline simulation run failed: "
        f"{FAILURE_MESSAGE}"
    )
    assert runs.of("fail_held_by") == []
    assert answer["status"] == "failed"


def test_a_held_side_whose_call_stopped_without_an_outcome_is_failed() -> None:
    runs, holder = _held_baseline("stops")

    answer = _run(runs, _succeeded, wait=_wait(holder))

    (failed_held,) = runs.of("fail_held_by")
    assert (failed_held["run"], failed_held["call"]) == (BASELINE_RUN_ID, "fc-earlier")
    assert failed_held["error_message"] == ENDED_CALL_MESSAGE
    (failed_report,) = runs.of("fail_report")
    assert failed_report["error_message"] == (
        "household_calculation_failed: The baseline simulation run failed: "
        f"{ENDED_CALL_MESSAGE}"
    )
    assert answer["status"] == "failed"


def test_a_failed_probe_of_a_running_holder_keeps_the_function_waiting() -> None:
    runs, holder = _held_baseline("succeeds")

    class LostConnectionOnce:
        object_id = holder.object_id
        failures = 1

        def get(self, *, timeout=None):
            if self.failures:
                self.failures -= 1
                msg = "the connection to Modal was reset"
                raise ConnectionError(msg)
            return holder.get(timeout=timeout)

        def cancel(self):
            holder.cancel()

    wait = _wait(holder, call_state=lambda _modal_call_id: CallState.RUNNING)
    wait = HeldRunWait(
        restore=lambda _modal_call_id: LostConnectionOnce(),
        call_state=wait.call_state,
        timeout_seconds=wait.timeout_seconds,
        sleep=wait.sleep,
        monotonic=wait.monotonic,
    )

    answer = _run(runs, _succeeded, wait=wait)

    assert answer["status"] == "succeeded"
    assert runs.of("fail_held_by") == []


def test_one_run_named_on_both_sides_is_calculated_once() -> None:
    runs = FakeHouseholdReportRuns(named_runs=(BASELINE_RUN_ID, BASELINE_RUN_ID))
    calculation = HouseholdReportRunCalculation.model_validate(
        {"baseline": _request(BASELINE_RUN_ID), "reform": _request(BASELINE_RUN_ID)}
    )
    calculated: list[UUID] = []

    answer = _run(runs, _recording_calculate(calculated), calculation=calculation)

    assert calculated == [BASELINE_RUN_ID]
    assert runs.kinds == [
        "claim_report",
        "read_runs",
        "claim",
        "complete",
        "complete_report",
    ]
    assert answer["status"] == "succeeded"
    assert [side["side"] for side in answer["simulation_runs"]] == [
        "baseline",
        "reform",
    ]
