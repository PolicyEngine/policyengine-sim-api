"""Reporting to the Simulation Entrypoint that a v2 Modal call ended."""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from policyengine_simulation_contract.call_reports import (
    CALL_ENDED_PATH,
    CallEndedReport,
)
from src.modal.call_reports import (
    CALL_REPORT_ATTEMPTS,
    ReportDelivery,
    reporting_call_end,
    send_call_ended_report,
    send_call_ended_report_from_container,
)

ENTRY_URL = "https://policyengine-simulation-entry-abc.a.run.app"
RUN_ID = uuid4()
REPORT = CallEndedReport(modal_call_id="fc-ended", simulation_run_ids=(RUN_ID,))


@dataclass
class _Entrypoint:
    """Answer each request with the next scripted (status, headers, body) or error."""

    answers: list
    requests: list = field(default_factory=list)

    def __call__(self, url, body, headers, timeout):
        self.requests.append({"url": url, "body": body, "headers": headers})
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        status, answer_headers, answer_body = answer
        return status, answer_headers, json.dumps(answer_body).encode()


@dataclass
class _Waits:
    seconds: list = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


def _send(answers, **settings):
    entrypoint, waits = _Entrypoint(list(answers)), _Waits()
    delivery = send_call_ended_report(
        REPORT,
        entry_url=settings.get("entry_url", ENTRY_URL),
        identity_token=settings.get("identity_token", "modal-identity-token"),
        post=entrypoint,
        sleep=waits,
    )
    return delivery, entrypoint, waits


def test_a_report_is_posted_with_the_containers_identity_token() -> None:
    delivery, entrypoint, waits = _send([(200, {}, {"status": "recorded"})])

    assert delivery is ReportDelivery.DELIVERED
    (request,) = entrypoint.requests
    assert request["url"] == ENTRY_URL + CALL_ENDED_PATH
    assert request["headers"]["Authorization"] == "Bearer modal-identity-token"
    assert request["headers"]["Content-Type"] == "application/json"
    assert CallEndedReport.model_validate_json(request["body"]) == REPORT
    assert waits.seconds == []


def test_a_report_sim_cannot_record_yet_is_sent_again_after_its_wait() -> None:
    delivery, entrypoint, waits = _send(
        [
            (503, {"Retry-After": "3"}, {"status": "still_running"}),
            (503, {}, {"status": "not_dispatched_yet"}),
            (200, {}, {"status": "already_recorded"}),
        ]
    )

    assert delivery is ReportDelivery.DELIVERED
    assert len(entrypoint.requests) == 3
    assert waits.seconds[0] == 3
    assert waits.seconds[1] > 0


@pytest.mark.parametrize(
    "failure",
    [urllib.error.URLError("unreachable"), TimeoutError(), ConnectionResetError()],
    ids=["unreachable", "timeout", "reset"],
)
def test_a_report_that_does_not_arrive_is_sent_again(failure) -> None:
    delivery, entrypoint, _ = _send([failure, (200, {}, {"status": "recorded"})])

    assert delivery is ReportDelivery.DELIVERED
    assert len(entrypoint.requests) == 2


@pytest.mark.parametrize("status", [401, 404, 409, 422])
def test_a_refused_report_is_not_sent_again(status) -> None:
    delivery, entrypoint, waits = _send([(status, {}, {"status": "other_call"})])

    assert delivery is ReportDelivery.REFUSED
    assert len(entrypoint.requests) == 1
    assert waits.seconds == []


def test_the_sender_gives_up_after_a_few_attempts_with_growing_waits() -> None:
    answers = [(503, {}, {"status": "still_running"})] * CALL_REPORT_ATTEMPTS

    delivery, entrypoint, waits = _send(answers)

    assert delivery is ReportDelivery.GAVE_UP
    assert len(entrypoint.requests) == CALL_REPORT_ATTEMPTS
    assert len(waits.seconds) == CALL_REPORT_ATTEMPTS - 1
    assert waits.seconds == sorted(waits.seconds)
    assert sum(waits.seconds) < 120


def test_an_unexpected_answer_is_not_a_crash() -> None:
    delivery, _, _ = _send([(200, {}, "not an answer")])

    assert delivery is ReportDelivery.DELIVERED


def test_a_report_without_sims_url_or_a_token_is_not_sent() -> None:
    delivery, entrypoint, _ = _send([], entry_url="")
    assert delivery is ReportDelivery.NOT_CONFIGURED
    delivery, entrypoint, _ = _send([], identity_token="")
    assert delivery is ReportDelivery.NOT_CONFIGURED
    assert entrypoint.requests == []


def test_the_report_function_reads_its_settings_from_the_container(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SIMULATION_ENTRY_URL", ENTRY_URL)
    monkeypatch.setenv("MODAL_IDENTITY_TOKEN", "modal-identity-token")
    entrypoint = _Entrypoint([(200, {}, {"status": "recorded"})])

    delivery = send_call_ended_report_from_container(
        REPORT.model_dump(mode="json"), post=entrypoint, sleep=_Waits()
    )

    assert delivery == "delivered"
    assert entrypoint.requests[0]["url"] == ENTRY_URL + CALL_ENDED_PATH


@dataclass
class _ReportStarter:
    """Stands in for ``report_call_ended.spawn``."""

    failure: BaseException | None = None
    started: list = field(default_factory=list)

    def __call__(self, report: dict) -> None:
        if self.failure is not None:
            raise self.failure
        self.started.append(report)


def test_a_call_that_returns_reports_its_end() -> None:
    starter = _ReportStarter()

    with reporting_call_end(
        modal_call_id="fc-ended", simulation_run_ids=(RUN_ID,), start_report=starter
    ):
        pass

    assert [CallEndedReport.model_validate(report) for report in starter.started] == [
        REPORT
    ]


def _failing_comparison(report_run_id, starter) -> None:
    with reporting_call_end(
        modal_call_id="fc-failed", report_run_id=report_run_id, start_report=starter
    ):
        msg = "calculation failed"
        raise ValueError(msg)


def test_a_call_that_fails_reports_its_end_and_still_fails() -> None:
    starter = _ReportStarter()
    report_run_id = uuid4()

    with pytest.raises(ValueError, match="calculation failed"):
        _failing_comparison(report_run_id, starter)

    assert starter.started == [
        {
            "modal_call_id": "fc-failed",
            "simulation_run_ids": None,
            "report_run_id": str(report_run_id),
        }
    ]


@pytest.mark.parametrize("signal", [KeyboardInterrupt(), SystemExit(1)])
def test_a_pre_empted_call_reports_nothing(signal) -> None:
    """Modal re-runs a pre-empted call under its ID, and the re-run reports."""
    starter = _ReportStarter()

    with (
        pytest.raises(type(signal)),
        reporting_call_end(
            modal_call_id="fc-ended", simulation_run_ids=(RUN_ID,), start_report=starter
        ),
    ):
        raise signal

    assert starter.started == []


def test_a_report_that_cannot_start_never_fails_the_calculation() -> None:
    starter = _ReportStarter(failure=RuntimeError("Modal unreachable"))

    with reporting_call_end(
        modal_call_id="fc-ended", simulation_run_ids=(RUN_ID,), start_report=starter
    ):
        pass


def test_a_call_without_a_modal_call_id_reports_nothing() -> None:
    """Run outside Modal (a local test), the call has no ID to report."""
    starter = _ReportStarter()

    with reporting_call_end(
        modal_call_id=None, simulation_run_ids=(RUN_ID,), start_report=starter
    ):
        pass

    assert starter.started == []
