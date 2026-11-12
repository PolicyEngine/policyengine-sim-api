"""The economy routes' answers, with the submission service faked."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, NoReturn
from uuid import UUID

import pytest
from conftest import FakeBackend, make_settings
from economy_service_fakes import REPORT_BODY, SIMULATION_BODY
from fastapi.testclient import TestClient
from policyengine_simulation_entry import app as app_module
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.economy_api_models import (
    EconomyReportCreated,
    EconomyReportRequest,
    EconomySimulationCreated,
    EconomySimulationRequest,
)
from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionRefusedError,
    EconomySubmissionUnavailableError,
    EconomySubmissionUnprocessableError,
    UnprocessableReason,
)
from policyengine_simulation_entry.economy_submissions import (
    EconomySubmissionService,
)
from policyengine_simulation_entry.simulation_database import SimulationDatabase
from policyengine_simulation_observability.identifiers import OBSERVABILITY_ID_HEADER
from policyengine_simulation_persistence import ReportRunStatus, SimulationStatus

OBSERVABILITY_ID = "6e1f3d4c-0a5b-4c39-9d2e-7f8a9b0c1d2e"
RECORD_ID = UUID("00000000-0000-4000-8000-0000000000e1")
RUN_ID = UUID("00000000-0000-4000-8000-0000000000e2")
SIDE_IDS = [UUID(f"00000000-0000-4000-8000-0000000000f{digit}") for digit in "1234"]
CREATED_REPORT = EconomyReportCreated(
    report_id=RECORD_ID,
    report_run_id=RUN_ID,
    status=ReportRunStatus.PENDING,
    baseline_simulation_id=SIDE_IDS[0],
    baseline_simulation_run_id=SIDE_IDS[1],
    baseline_reused=True,
    reform_simulation_id=SIDE_IDS[2],
    reform_simulation_run_id=SIDE_IDS[3],
    reform_reused=False,
)
CREATED_SIMULATION = EconomySimulationCreated(
    simulation_id=RECORD_ID, simulation_run_id=RUN_ID, status=SimulationStatus.PENDING
)
UNEXPECTED_FAILURE_ANSWER = {
    "code": "internal_error",
    "detail": "The service could not answer the request.",
}


def _raise(failure: Exception) -> NoReturn:
    raise failure


@dataclass
class FakeEconomySubmissions:
    """Records each submission, and answers as told."""

    failure: Exception | None = None
    reports: list[tuple[EconomyReportRequest, str, Any]] = field(default_factory=list)
    simulations: list[tuple[EconomySimulationRequest, str, Any]] = field(
        default_factory=list
    )

    async def submit_report(
        self,
        request: EconomyReportRequest,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomyReportCreated:
        self.reports.append((request, request_id, observability_context))
        if self.failure is not None:
            _raise(self.failure)
        return CREATED_REPORT

    async def submit_simulation(
        self,
        request: EconomySimulationRequest,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomySimulationCreated:
        self.simulations.append((request, request_id, observability_context))
        if self.failure is not None:
            _raise(self.failure)
        return CREATED_SIMULATION


@pytest.fixture
def client_for(monkeypatch):
    """Return a function building a client whose app uses ``submissions``.

    None leaves the app as an unconfigured service builds it.
    """

    def build(submissions: FakeEconomySubmissions | None) -> TestClient:
        if submissions is not None:
            monkeypatch.setattr(
                app_module,
                "economy_submissions_for",
                lambda settings, database: submissions,
            )
        return TestClient(
            create_app(
                settings=make_settings(),
                backend=FakeBackend(),
                auth_dependency=lambda: None,
            )
        )

    return build


ROUTES = [("/reports", REPORT_BODY), ("/simulations", SIMULATION_BODY)]


def test_a_report_answers_201_with_its_records_and_runs(client_for) -> None:
    submissions = FakeEconomySubmissions()

    with client_for(submissions) as client:
        result = client.post(
            "/reports",
            json=REPORT_BODY,
            headers={OBSERVABILITY_ID_HEADER: OBSERVABILITY_ID},
        )

    assert result.status_code == 201
    assert result.json() == CREATED_REPORT.model_dump(mode="json")
    assert result.headers[OBSERVABILITY_ID_HEADER] == OBSERVABILITY_ID
    ((request, request_id, context),) = submissions.reports
    assert request.label == REPORT_BODY["label"]
    assert request.options() == {
        "include_cliffs": True,
        "spm": {"geography_kind": "national"},
    }
    assert request_id == OBSERVABILITY_ID
    assert context["observability_id"] == OBSERVABILITY_ID


def test_a_simulation_answers_201_with_its_record_and_run(client_for) -> None:
    submissions = FakeEconomySubmissions()

    with client_for(submissions) as client:
        result = client.post("/simulations", json=SIMULATION_BODY)

    assert result.status_code == 201
    assert result.json() == {
        "simulation_id": str(RECORD_ID),
        "simulation_run_id": str(RUN_ID),
        "status": "pending",
    }
    ((request, request_id, _),) = submissions.simulations
    assert str(request.policy_id) == SIMULATION_BODY["policy_id"]
    assert request_id == result.headers[OBSERVABILITY_ID_HEADER]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/reports", {**REPORT_BODY, "report_type": "household"}),
        ("/reports", {**REPORT_BODY, "spm": None}),
        ("/reports", {**REPORT_BODY, "include_cliffs": "yes"}),
        ("/reports", {**REPORT_BODY, "report_id": str(RECORD_ID)}),
        ("/reports", {**REPORT_BODY, "label": ""}),
        ("/reports", {**REPORT_BODY, "year": 2201}),
        ("/reports", {**REPORT_BODY, "policyengine_version": "6.17.1"}),
        ("/reports", {**REPORT_BODY, "reform": {"policy_id": "not-a-uuid"}}),
        ("/simulations", {**SIMULATION_BODY, "country": "fr"}),
        ("/simulations", {**SIMULATION_BODY, "region_code": "state ca"}),
        ("/simulations", {**SIMULATION_BODY, "simulation_id": str(RECORD_ID)}),
        ("/simulations", {**SIMULATION_BODY, "simulation_type": "household"}),
    ],
)
def test_a_malformed_request_is_refused_with_its_reasons(client_for, path, body):
    submissions = FakeEconomySubmissions()

    with client_for(submissions) as client:
        result = client.post(path, json=body)

    assert result.status_code == 422
    assert all({"loc", "msg"} <= set(reason) for reason in result.json()["detail"])
    assert (submissions.reports, submissions.simulations) == ([], [])


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_a_refusal_answers_400_with_its_code(client_for, path, body) -> None:
    submissions = FakeEconomySubmissions(
        failure=EconomySubmissionRefusedError(
            code="unknown_region", detail="no region state/zz"
        )
    )

    with client_for(submissions) as client:
        result = client.post(path, json=body)

    assert result.status_code == 400
    assert result.json() == {"code": "unknown_region", "detail": "no region state/zz"}


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_stored_values_that_cannot_be_calculated_answer_422_with_a_code(
    client_for, path, body
) -> None:
    submissions = FakeEconomySubmissions(
        failure=EconomySubmissionUnprocessableError(
            code="missing_parameters",
            reasons=(
                UnprocessableReason(
                    loc=("body", "policy_id", "gov.a"),
                    msg="no such parameter",
                    type="missing_parameter",
                ),
            ),
        )
    )

    with client_for(submissions) as client:
        result = client.post(path, json=body)

    assert result.status_code == 422
    assert result.json() == {
        "detail": [
            {
                "loc": ["body", "policy_id", "gov.a"],
                "msg": "no such parameter",
                "type": "missing_parameter",
            }
        ],
        "code": "missing_parameters",
    }


@pytest.mark.parametrize(
    ("path", "body", "record_field"),
    [
        ("/reports", REPORT_BODY, "report_id"),
        ("/simulations", SIMULATION_BODY, "simulation_id"),
    ],
)
def test_a_refused_call_answers_503_naming_the_record_it_created(
    client_for, path, body, record_field
) -> None:
    submissions = FakeEconomySubmissions(
        failure=EconomySubmissionUnavailableError(
            code="modal_call_refused", detail="Modal refused", record_id=RECORD_ID
        )
    )

    with client_for(submissions) as client:
        result = client.post(path, json=body)

    assert result.status_code == 503
    assert result.headers["Retry-After"] == "10"
    assert result.json() == {
        "code": "modal_call_refused",
        "detail": "Modal refused",
        record_field: str(RECORD_ID),
    }


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_an_unavailable_dependency_answers_503_naming_no_record(
    client_for, path, body
) -> None:
    submissions = FakeEconomySubmissions(
        failure=EconomySubmissionUnavailableError(
            code="database_unavailable", detail="The database could not be read."
        )
    )

    with client_for(submissions) as client:
        result = client.post(path, json=body)

    assert result.status_code == 503
    assert result.json() == {
        "code": "database_unavailable",
        "detail": "The database could not be read.",
    }


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_an_unconfigured_service_answers_503(client_for, path, body) -> None:
    with client_for(None) as client:
        result = client.post(path, json=body)

    assert result.status_code == 503
    assert result.headers["Retry-After"] == "10"
    assert result.json() == {
        "code": "not_configured",
        "detail": "Economy runs are not configured.",
    }


def _emitted_records(printed: str, message: str) -> list[dict[str, Any]]:
    """Return the records with ``message`` the observability runtime printed."""
    records = [
        json.loads(printed_line)
        for printed_line in printed.splitlines()
        if printed_line.startswith("{")
    ]
    return [record for record in records if record.get("message") == message]


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_an_unexpected_failure_answers_a_json_500_without_its_values(
    client_for, capsys, path, body
) -> None:
    submissions = FakeEconomySubmissions(
        failure=RuntimeError("a value that must not be logged")
    )

    with client_for(submissions) as client:
        result = client.post(path, json=body)

    assert result.status_code == 500
    assert result.json() == UNEXPECTED_FAILURE_ANSWER
    printed = capsys.readouterr().out
    (record,) = _emitted_records(printed, "economy_route_failed")
    attributes = record["attributes"]
    assert (attributes["route"], attributes["error_type"]) == (path, "RuntimeError")
    assert "test_economy_routes.py" in attributes["stack_frames"].split("; ")[0]
    assert "a value that must not be logged" not in printed


DATABASE_URL = "postgresql://simulation_service:secret@db.example:5432/postgres"


def test_a_configured_service_builds_economy_submissions_without_connecting():
    """Nothing reaches the database or Modal until the first submission."""
    database = SimulationDatabase.from_url(DATABASE_URL)
    try:
        submissions = app_module.economy_submissions_for(
            make_settings(
                simulation_database_url=DATABASE_URL,
                v2_modal_environment="staging",
                v2_simulation_output_bucket="policyengine-v2-outputs-staging",
            ),
            database,
        )
    finally:
        database.dispose()

    assert isinstance(submissions, EconomySubmissionService)


@pytest.mark.parametrize(
    "settings_changes",
    [
        {},
        {"simulation_database_url": DATABASE_URL},
        {"v2_modal_environment": "staging"},
    ],
)
def test_without_its_database_and_v2_environment_none_is_built(settings_changes):
    database = SimulationDatabase.from_url(DATABASE_URL)
    try:
        submissions = app_module.economy_submissions_for(
            make_settings(**settings_changes), database
        )
    finally:
        database.dispose()

    assert submissions is None
