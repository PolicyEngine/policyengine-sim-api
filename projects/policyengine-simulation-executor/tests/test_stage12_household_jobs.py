"""A household function records the household job it is given.

It claims the job under its Modal call ID, calculates the household, then
records the calculation's result or failure on the job. A job another call
holds, or one already finished, is not calculated again. Modal pre-empts a
call with SIGINT, which a synchronous function receives as
``KeyboardInterrupt``, and runs it again under the same call ID, so a
pre-emption records nothing.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from policyengine_simulation_contract.household_execution import (
    HouseholdCalculationError,
    HouseholdExecutionFailure,
    HouseholdExecutionResult,
    HouseholdWorkerIdentity,
)
from policyengine_simulation_executor.stage12_runtime import household_job_recording
from policyengine_simulation_executor.stage12_runtime.household_job_recording import (
    MAXIMUM_FAILURE_SUMMARY_LENGTH,
    household_failure_summary,
    run_household_job,
)
from policyengine_stage12_persistence import RunStatus, RunWrite

HOUSEHOLD_JOB_ID = UUID("00000000-0000-4000-8000-00000000a001")
CALCULATION_ID = UUID("00000000-0000-4000-8000-0000000000c1")
CALL_ID = "fc-household"
WORKER = HouseholdWorkerIdentity(
    application_name="policyengine-simulation-v2-py6-7-5",
    function_name="calculate_household_us",
)


class FakeHouseholdJobs:
    """Records each household job write; a test sets what each write returns."""

    def __init__(  # noqa: PLR0913  # what each of the three writes returns or raises
        self,
        claimed: RunWrite | None = None,
        *,
        claim_error: BaseException | None = None,
        completed: RunWrite | None = None,
        complete_error: BaseException | None = None,
        failed: RunWrite | None = None,
        fail_error: BaseException | None = None,
    ) -> None:
        self.claimed = claimed or RunWrite(changed=True, status=RunStatus.RUNNING)
        self.claim_error = claim_error
        self.completed = completed or RunWrite(changed=True, status=RunStatus.SUCCEEDED)
        self.complete_error = complete_error
        self.failed = failed or RunWrite(changed=True, status=RunStatus.FAILED)
        self.fail_error = fail_error
        self.writes: list[dict] = []

    def claim_household_job(self, household_job_id, *, modal_call_id, claimed_at):
        self.writes.append(
            {"write": "claim", "job": household_job_id, "call": modal_call_id}
        )
        if self.claim_error is not None:
            raise self.claim_error
        return self.claimed

    def complete_household_job(
        self, household_job_id, *, modal_call_id, result, completed_at
    ):
        self.writes.append(
            {
                "write": "complete",
                "job": household_job_id,
                "call": modal_call_id,
                "result": result,
            }
        )
        if self.complete_error is not None:
            raise self.complete_error
        return self.completed

    def fail_household_job(
        self, household_job_id, *, modal_call_id, error_message, failed_at, result=None
    ):
        self.writes.append(
            {
                "write": "fail",
                "job": household_job_id,
                "call": modal_call_id,
                "error_message": error_message,
                "result": result,
            }
        )
        if self.fail_error is not None:
            raise self.fail_error
        return self.failed

    @property
    def kinds(self) -> list[str]:
        return [write["write"] for write in self.writes]


class InputCancelledForTest(BaseException):
    """Stands in for Modal's InputCancellation, a BaseException but not an Exception."""


def _succeeded() -> HouseholdExecutionResult:
    return HouseholdExecutionResult.model_validate(
        {
            "calculation_id": str(CALCULATION_ID),
            "output": {
                "country": "us",
                "bundle": {
                    "policyengine_version": "6.7.5",
                    "core_package_version": "3.33.0",
                    "country_package_name": "policyengine-us",
                    "country_package_version": "2.2.1",
                    "bundle_manifest_sha256": "a" * 64,
                },
                "results": [
                    {
                        "policy_key": "baseline",
                        "household": {"people": {"you": {"age": {"2026": 40}}}},
                    }
                ],
            },
            "worker": WORKER.model_dump(),
        }
    )


def _failed(
    message: str = "Unrecognized calculate input(s): x",
) -> HouseholdExecutionFailure:
    return HouseholdExecutionFailure(
        calculation_id=CALCULATION_ID,
        error=HouseholdCalculationError(code="invalid_inputs", message=message),
        worker=WORKER,
    )


def _run(jobs, calculate, *, modal_call_id=CALL_ID):
    return run_household_job(
        HOUSEHOLD_JOB_ID, calculate, modal_call_id=modal_call_id, writer=jobs
    )


def test_a_job_is_claimed_under_the_call_then_completed_with_its_result() -> None:
    jobs = FakeHouseholdJobs()
    outcome = _succeeded()

    returned = _run(jobs, lambda: outcome)

    assert returned == outcome.model_dump(mode="json")
    assert jobs.writes == [
        {"write": "claim", "job": HOUSEHOLD_JOB_ID, "call": CALL_ID},
        {
            "write": "complete",
            "job": HOUSEHOLD_JOB_ID,
            "call": CALL_ID,
            "result": outcome.model_dump(mode="json"),
        },
    ]


def test_a_failed_calculation_fails_the_job_with_its_document_and_summary() -> None:
    jobs = FakeHouseholdJobs()
    outcome = _failed("Unrecognized calculate input(s):\n  x;\n  y")

    returned = _run(jobs, lambda: outcome)

    assert returned == outcome.model_dump(mode="json")
    assert jobs.kinds == ["claim", "fail"]
    failure = jobs.writes[1]
    assert failure["call"] == CALL_ID
    assert failure["result"] == outcome.model_dump(mode="json")
    assert (
        failure["error_message"]
        == "invalid_inputs: Unrecognized calculate input(s): x; y"
    )


def test_a_failure_summary_is_one_line_of_bounded_length() -> None:
    summary = household_failure_summary(_failed("a" * 5_000))

    assert len(summary) == MAXIMUM_FAILURE_SUMMARY_LENGTH
    assert summary.startswith("invalid_inputs: aaa")
    assert summary.endswith("...")


@pytest.mark.parametrize(
    "status", [RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.FAILED]
)
def test_a_job_another_call_holds_or_that_finished_is_not_calculated(status) -> None:
    jobs = FakeHouseholdJobs(RunWrite(changed=False, status=status))
    calculated = []

    returned = _run(jobs, lambda: calculated.append(True))

    assert returned == {
        "deduplicated": True,
        "household_job_id": str(HOUSEHOLD_JOB_ID),
        "status": status.value,
    }
    assert calculated == []
    assert jobs.kinds == ["claim"]


def test_a_pending_job_that_records_another_call_is_not_calculated() -> None:
    # The simulation service recorded another call on the pending job, so the
    # store refuses this call's claim and the job stays pending for that call.
    jobs = FakeHouseholdJobs(RunWrite(changed=False, status=RunStatus.PENDING))
    calculated = []

    returned = _run(jobs, lambda: calculated.append(True))

    assert returned == {
        "deduplicated": True,
        "household_job_id": str(HOUSEHOLD_JOB_ID),
        "status": "pending",
    }
    assert calculated == []
    assert jobs.kinds == ["claim"]


def test_a_failed_completion_write_fails_the_job_and_propagates() -> None:
    # The calculation succeeded, but recording it raised: the job is failed
    # with a generic summary and no result, and the write's error propagates.
    jobs = FakeHouseholdJobs(complete_error=ConnectionError("connection lost"))

    with pytest.raises(ConnectionError, match="connection lost"):
        _run(jobs, _succeeded)

    assert jobs.kinds == ["claim", "complete", "fail"]
    failure = jobs.writes[2]
    assert failure["call"] == CALL_ID
    assert failure["result"] is None
    assert failure["error_message"] == (
        "household_function_error: The household function raised ConnectionError."
    )


@pytest.mark.parametrize(
    ("outcome_of", "unchanged"),
    [
        (_succeeded, "completed"),
        (_failed, "failed"),
    ],
    ids=["succeeded", "failed"],
)
def test_an_outcome_write_that_changes_nothing_still_returns_the_outcome(
    caplog, outcome_of, unchanged
) -> None:
    # The sweep failed the job while it was calculated, so recording the
    # outcome changes nothing; the call logs that and returns the outcome.
    unchanged_write = RunWrite(changed=False, status=RunStatus.FAILED)
    jobs = FakeHouseholdJobs(**{unchanged: unchanged_write})
    outcome = outcome_of()

    with caplog.at_level("WARNING", logger=household_job_recording.__name__):
        returned = _run(jobs, lambda: outcome)

    assert returned == outcome.model_dump(mode="json")
    assert len(jobs.writes) == 2
    (warning,) = caplog.records
    assert warning.levelname == "WARNING"
    assert warning.getMessage() == (
        f"Household job {HOUSEHOLD_JOB_ID} was failed before its calculation "
        "was recorded"
    )


def test_an_unexpected_error_fails_the_job_with_a_generic_summary_and_propagates() -> (
    None
):
    jobs = FakeHouseholdJobs()

    def calculate():
        msg = "a detail of the request"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="a detail of the request"):
        _run(jobs, calculate)

    assert jobs.kinds == ["claim", "fail"]
    failure = jobs.writes[1]
    assert failure["call"] == CALL_ID
    assert failure["result"] is None
    assert failure["error_message"] == (
        "household_function_error: The household function raised ValueError."
    )


def test_a_cancellation_fails_the_job_and_propagates() -> None:
    jobs = FakeHouseholdJobs()

    def calculate():
        raise InputCancelledForTest

    with pytest.raises(InputCancelledForTest):
        _run(jobs, calculate)

    assert jobs.kinds == ["claim", "fail"]
    assert jobs.writes[1]["error_message"] == (
        "household_function_cancelled: Modal cancelled the household function's call."
    )


def test_a_preemption_records_nothing_because_modal_runs_the_call_again() -> None:
    jobs = FakeHouseholdJobs()

    def calculate():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run(jobs, calculate)

    assert jobs.kinds == ["claim"]


def test_a_failed_failure_write_leaves_the_original_error_to_propagate() -> None:
    jobs = FakeHouseholdJobs(fail_error=ConnectionError("database unavailable"))

    def calculate():
        msg = "calculation error"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="calculation error"):
        _run(jobs, calculate)

    assert jobs.kinds == ["claim", "fail"]


def test_a_claim_error_fails_the_job_quietly_and_propagates() -> None:
    # The claim may have committed before the error; failing a job this call
    # doesn't hold changes nothing.
    jobs = FakeHouseholdJobs(claim_error=ConnectionError("connection lost"))
    calculated = []

    with pytest.raises(ConnectionError, match="connection lost"):
        _run(jobs, lambda: calculated.append(True))

    assert calculated == []
    assert jobs.kinds == ["claim", "fail"]
    assert jobs.writes[1]["error_message"] == (
        "household_function_error: The household function raised ConnectionError."
    )


def test_a_job_cannot_be_recorded_without_the_modal_call_id() -> None:
    jobs = FakeHouseholdJobs()
    calculated = []

    with pytest.raises(RuntimeError, match="call identifier is unavailable"):
        _run(jobs, lambda: calculated.append(True), modal_call_id=None)

    assert jobs.writes == []
    assert calculated == []


def test_the_job_store_is_built_from_the_modal_worker_database_url(
    monkeypatch,
) -> None:
    jobs = FakeHouseholdJobs()
    runtime = object()
    built_with = []

    def report_run_store(received_runtime):
        built_with.append(received_runtime)
        return jobs

    monkeypatch.setattr(household_job_recording, "report_run_store", report_run_store)

    run_household_job(
        HOUSEHOLD_JOB_ID, _succeeded, modal_call_id=CALL_ID, runtime=runtime
    )

    assert built_with == [runtime]
    assert jobs.kinds == ["claim", "complete"]
