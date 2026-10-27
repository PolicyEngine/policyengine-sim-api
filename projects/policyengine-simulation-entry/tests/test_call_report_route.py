"""The route on which sim's call-report Modal function reports that a call ended."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from conftest import FakeBackend, make_settings
from fastapi import HTTPException
from fastapi.testclient import TestClient
from policyengine_simulation_contract.call_reports import (
    CALL_ENDED_PATH,
    RETRY_LATER_STATUSES,
    CallEndedReport,
    CallReportStatus,
)
from policyengine_simulation_entry.app import RunServices, create_app
from policyengine_simulation_entry.modal_identity import (
    ModalIdentity,
    ModalIdentityError,
    ModalKeysUnavailableError,
)

GOOD_TOKEN = "token-of-the-call-report-function"
RUN_ID = uuid4()
REPORT = {"modal_call_id": "fc-ended", "simulation_run_ids": [str(RUN_ID)]}


@dataclass
class _Verifier:
    """Accept one token; refuse or fail on the others."""

    keys_unavailable: bool = False
    verified: list[str] = field(default_factory=list)

    def verify(self, token: str) -> ModalIdentity:
        self.verified.append(token)
        if self.keys_unavailable:
            raise ModalKeysUnavailableError
        if token != GOOD_TOKEN:
            msg = "the token names another Modal function"
            raise ModalIdentityError(msg)
        return ModalIdentity(
            app_name="policyengine-simulation-v2-py6-10-0",
            function_name="report_call_ended",
            container_id="ta-container0001",
        )


@dataclass
class _Recorder:
    """Answer every report with one status, and keep the reports."""

    status: CallReportStatus = CallReportStatus.RECORDED
    reports: list[CallEndedReport] = field(default_factory=list)

    async def record_reported_call(self, report: CallEndedReport) -> CallReportStatus:
        self.reports.append(report)
        return self.status


def _reject_every_caller():
    raise HTTPException(status_code=403)


def _client(verifier=None, recorder=None) -> TestClient:
    return TestClient(
        create_app(
            settings=make_settings(),
            backend=FakeBackend(),
            auth_dependency=_reject_every_caller,
            run_services=RunServices(
                call_recorder=recorder, modal_identity_verifier=verifier
            ),
        )
    )


def _post(client, body=REPORT, token=GOOD_TOKEN):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return client.post(CALL_ENDED_PATH, json=body, headers=headers)


def test_a_report_from_the_call_report_function_is_recorded() -> None:
    verifier, recorder = _Verifier(), _Recorder()

    response = _post(_client(verifier, recorder))

    assert response.status_code == 200
    assert response.json() == {"status": "recorded"}
    assert verifier.verified == [GOOD_TOKEN]
    assert recorder.reports == [CallEndedReport.model_validate(REPORT)]


def test_a_repeated_report_is_answered_as_already_recorded() -> None:
    recorder = _Recorder(status=CallReportStatus.ALREADY_RECORDED)

    response = _post(_client(_Verifier(), recorder))

    assert response.status_code == 200
    assert response.json() == {"status": "already_recorded"}


@pytest.mark.parametrize("status", sorted(RETRY_LATER_STATUSES - {"not_configured"}))
def test_a_report_sim_cannot_record_yet_is_answered_with_a_wait(status) -> None:
    response = _post(_client(_Verifier(), _Recorder(status=CallReportStatus(status))))

    assert response.status_code == 503
    assert response.json() == {"status": status}
    assert int(response.headers["Retry-After"]) > 0


@pytest.mark.parametrize(
    ("status", "http_status"),
    [(CallReportStatus.UNKNOWN_RUNS, 404), (CallReportStatus.OTHER_CALL, 409)],
)
def test_a_report_that_cannot_be_recorded_is_refused(status, http_status) -> None:
    response = _post(_client(_Verifier(), _Recorder(status=status)))

    assert response.status_code == http_status
    assert response.json() == {"status": status.value}
    assert "Retry-After" not in response.headers


@pytest.mark.parametrize("token", [None, "token-of-a-policy-worker"])
def test_a_request_without_the_functions_token_is_refused(token) -> None:
    recorder = _Recorder()

    response = _post(_client(_Verifier(), recorder), token=token)

    assert response.status_code == 401
    assert recorder.reports == []


def test_an_unauthenticated_request_learns_nothing_about_its_body() -> None:
    response = _post(_client(_Verifier(), _Recorder()), body={"x": 1}, token=None)

    assert response.status_code == 401


def test_a_malformed_report_is_refused_after_authentication() -> None:
    recorder = _Recorder()

    response = _post(
        _client(_Verifier(), recorder),
        body={"modal_call_id": "fc-ended", "simulation_run_ids": [], "result": {}},
    )

    assert response.status_code == 422
    assert recorder.reports == []


def test_unreachable_modal_keys_are_answered_with_a_wait() -> None:
    recorder = _Recorder()

    response = _post(_client(_Verifier(keys_unavailable=True), recorder))

    assert response.status_code == 503
    assert response.json() == {"status": "modal_unreachable"}
    assert recorder.reports == []


@pytest.mark.parametrize(
    ("verifier", "recorder"),
    [(None, _Recorder()), (_Verifier(), None)],
    ids=["no-modal-caller", "no-database"],
)
def test_an_instance_not_configured_for_reports_answers_with_a_wait(
    verifier, recorder
) -> None:
    response = _post(_client(verifier, recorder))

    assert response.status_code == 503
    assert response.json() == {"status": "not_configured"}


def test_the_route_is_not_in_the_public_openapi_document() -> None:
    document = _client(_Verifier(), _Recorder()).get("/openapi.json").json()

    assert CALL_ENDED_PATH not in document["paths"]


class _FailingVerifier:
    def verify(self, token: str) -> ModalIdentity:
        msg = "an error the verifier did not expect"
        raise RuntimeError(msg)


def test_an_unexpected_verifier_failure_is_answered_with_a_wait() -> None:
    """The sender reports again, instead of taking a 500 as a refusal."""
    recorder = _Recorder()

    response = _post(_client(_FailingVerifier(), recorder))

    assert response.status_code == 503
    assert response.json() == {"status": "modal_unreachable"}
    assert recorder.reports == []
