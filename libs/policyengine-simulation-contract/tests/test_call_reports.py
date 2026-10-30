"""The report a Modal function's call sends sim when it ends, and sim's answer."""

from __future__ import annotations

from uuid import uuid4

import pytest
from policyengine_simulation_contract.call_reports import (
    RETRY_LATER_STATUSES,
    CallEndedReport,
    CallReportAnswer,
    CallReportStatus,
)
from pydantic import ValidationError

CALL_ID = "fc-01JB3W7X9Z8Q2R5T6Y4U1I0O9P"


def test_a_report_names_the_simulation_runs_its_call_computes() -> None:
    baseline, reform = uuid4(), uuid4()

    report = CallEndedReport(
        modal_call_id=CALL_ID, simulation_run_ids=(baseline, reform)
    )

    assert report.simulation_run_ids == (baseline, reform)
    assert report.report_run_id is None


def test_a_report_names_the_report_run_whose_comparison_it_computes() -> None:
    report_run_id = uuid4()

    report = CallEndedReport(modal_call_id=CALL_ID, report_run_id=report_run_id)

    assert report.report_run_id == report_run_id
    assert report.simulation_run_ids is None


def test_a_report_round_trips_as_json() -> None:
    report = CallEndedReport(modal_call_id=CALL_ID, simulation_run_ids=(uuid4(),))

    assert CallEndedReport.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"simulation_run_ids": (uuid4(),), "report_run_id": uuid4()},
        {"simulation_run_ids": ()},
        {"simulation_run_ids": (uuid4(), uuid4(), uuid4())},
    ],
    ids=["neither", "both", "no-runs", "three-runs"],
)
def test_a_report_names_either_one_or_two_simulation_runs_or_one_report_run(
    fields,
) -> None:
    with pytest.raises(ValidationError):
        CallEndedReport(modal_call_id=CALL_ID, **fields)


def test_a_report_names_each_simulation_run_once() -> None:
    run_id = uuid4()

    with pytest.raises(ValidationError, match="twice"):
        CallEndedReport(modal_call_id=CALL_ID, simulation_run_ids=(run_id, run_id))


@pytest.mark.parametrize(
    "call_id", ["", "fc-", "in-01JB3W7X9Z", "fc-01JB/../x", "fc-" + "A" * 80]
)
def test_a_report_names_a_modal_function_call(call_id) -> None:
    with pytest.raises(ValidationError):
        CallEndedReport(modal_call_id=call_id, report_run_id=uuid4())


def test_a_report_carries_nothing_else() -> None:
    with pytest.raises(ValidationError):
        CallEndedReport.model_validate(
            {"modal_call_id": CALL_ID, "report_run_id": str(uuid4()), "result": {}}
        )


def test_the_sender_retries_only_while_sim_cannot_record_yet() -> None:
    assert {
        CallReportStatus.NOT_DISPATCHED_YET,
        CallReportStatus.STILL_RUNNING,
        CallReportStatus.MODAL_UNREACHABLE,
        CallReportStatus.NO_RESULT_RECORDER,
        CallReportStatus.NOT_WRITTEN,
        CallReportStatus.NOT_CONFIGURED,
    } == RETRY_LATER_STATUSES
    assert CallReportStatus.RECORDED not in RETRY_LATER_STATUSES
    assert CallReportStatus.UNKNOWN_RUNS not in RETRY_LATER_STATUSES


def test_an_answer_says_whether_to_report_again() -> None:
    assert CallReportAnswer(status=CallReportStatus.STILL_RUNNING).retry_later
    assert not CallReportAnswer(status=CallReportStatus.ALREADY_RECORDED).retry_later
