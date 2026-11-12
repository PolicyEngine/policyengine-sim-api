"""The economy routes' answers, with the submission service faked."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, NoReturn
from uuid import UUID

import pytest
from conftest import FakeBackend, make_settings
from economy_service_fakes import REPORT_BODY, SIMULATION_BODY
from fastapi.testclient import TestClient
from policyengine_observability import REQUEST_ID_HEADER
from policyengine_simulation_entry import app as app_module
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.economy_api_models import (
    EconomyReportCreated,
    EconomyReportRequest,
    EconomySimulationCreated,
    EconomySimulationRequest,
)
from policyengine_simulation_entry.economy_routes import call_request_id_for
from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionOutcomeUnknownError,
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
from policyengine_simulation_persistence import (
    EconomyReportState,
    EconomySimulationState,
    ReportRunState,
    ReportRunStatus,
    SimulationStatus,
)

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
    stored_report: EconomyReportState | None = None
    stored_simulation: EconomySimulationState | None = None
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

    async def read_report(self, report_id: UUID) -> EconomyReportState | None:
        if self.failure is not None:
            _raise(self.failure)
        stored = self.stored_report
        return stored if stored is not None and stored.id == report_id else None

    async def read_simulation(
        self, simulation_id: UUID
    ) -> EconomySimulationState | None:
        if self.failure is not None:
            _raise(self.failure)
        stored = self.stored_simulation
        return stored if stored is not None and stored.id == simulation_id else None


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
    # The call's request ID is the request's own (see the request ID test).
    assert request_id != OBSERVABILITY_ID
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
    assert request_id != result.headers[OBSERVABILITY_ID_HEADER]


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


READ_AT = "2026-11-12T15:00:00Z"
STORED_REPORT = EconomyReportState(
    id=RECORD_ID,
    country="uk",
    baseline_simulation_id=SIDE_IDS[0],
    reform_simulation_id=SIDE_IDS[2],
    run=ReportRunState(
        id=RUN_ID,
        status=ReportRunStatus.FAILED,
        country_package_version="2.174.1",
        policyengine_version="6.17.1",
        error_message="simulation_failed: the reform failed",
        created_at=datetime.fromisoformat(READ_AT),
        started_at=datetime.fromisoformat(READ_AT),
        completed_at=datetime.fromisoformat(READ_AT),
        baseline_simulation_run_id=SIDE_IDS[1],
        reform_simulation_run_id=SIDE_IDS[3],
    ),
)
STORED_SIMULATION = EconomySimulationState(id=RECORD_ID, run=None)


def test_a_report_is_read_with_its_newest_runs_status(client_for) -> None:
    submissions = FakeEconomySubmissions(stored_report=STORED_REPORT)

    with client_for(submissions) as client:
        result = client.get(f"/reports/{RECORD_ID}")
        missing = client.get(f"/reports/{RUN_ID}")

    assert result.status_code == 200
    assert result.json() == {
        "report_id": str(RECORD_ID),
        "country": "uk",
        "baseline_simulation_id": str(SIDE_IDS[0]),
        "reform_simulation_id": str(SIDE_IDS[2]),
        "run": {
            "run_id": str(RUN_ID),
            "status": "failed",
            "country_package_version": "2.174.1",
            "policyengine_version": "6.17.1",
            "error_message": "simulation_failed: the reform failed",
            "created_at": READ_AT,
            "started_at": READ_AT,
            "completed_at": READ_AT,
            "baseline_simulation_run_id": str(SIDE_IDS[1]),
            "reform_simulation_run_id": str(SIDE_IDS[3]),
        },
    }
    assert missing.status_code == 404
    assert missing.json() == {
        "code": "unknown_report",
        "detail": f"There is no report {RUN_ID}.",
    }


def test_a_simulation_is_read_with_its_newest_runs_status(client_for) -> None:
    submissions = FakeEconomySubmissions(stored_simulation=STORED_SIMULATION)

    with client_for(submissions) as client:
        result = client.get(f"/simulations/{RECORD_ID}")
        missing = client.get(f"/simulations/{RUN_ID}")

    assert result.status_code == 200
    assert result.json() == {"simulation_id": str(RECORD_ID), "run": None}
    assert missing.status_code == 404
    assert missing.json()["code"] == "unknown_simulation"


@pytest.mark.parametrize("path", ["/reports/not-a-uuid", "/simulations/not-a-uuid"])
def test_a_read_of_an_id_that_is_not_a_uuid_is_refused(client_for, path) -> None:
    with client_for(FakeEconomySubmissions()) as client:
        result = client.get(path)

    assert result.status_code == 422


@pytest.mark.parametrize("record", ["reports", "simulations"])
def test_a_read_the_database_cannot_answer_is_unavailable(client_for, record) -> None:
    submissions = FakeEconomySubmissions(
        failure=EconomySubmissionUnavailableError(
            code="database_unavailable", detail="The database could not be read."
        )
    )

    with client_for(submissions) as client:
        result = client.get(f"/{record}/{RECORD_ID}")

    assert result.status_code == 503
    assert result.headers["Retry-After"] == "10"
    assert result.json() == {
        "code": "database_unavailable",
        "detail": "The database could not be read.",
    }


@pytest.mark.parametrize("record", ["reports", "simulations"])
def test_an_unconfigured_service_answers_reads_with_503(client_for, record) -> None:
    with client_for(None) as client:
        result = client.get(f"/{record}/{RECORD_ID}")

    assert result.status_code == 503
    assert result.json()["code"] == "not_configured"


@pytest.mark.parametrize("record", ["reports", "simulations"])
def test_an_unexpected_read_failure_answers_a_json_500(client_for, record) -> None:
    submissions = FakeEconomySubmissions(failure=RuntimeError("must not be logged"))

    with client_for(submissions) as client:
        result = client.get(f"/{record}/{RECORD_ID}")

    assert result.status_code == 500
    assert result.json() == UNEXPECTED_FAILURE_ANSWER


def test_the_calls_request_id_is_the_requests_own(client_for) -> None:
    """The observability ID travels in the observability context instead."""
    submissions = FakeEconomySubmissions()

    with client_for(submissions) as client:
        client.post(
            "/reports",
            json=REPORT_BODY,
            headers={
                REQUEST_ID_HEADER: "request-1",
                OBSERVABILITY_ID_HEADER: OBSERVABILITY_ID,
            },
        )
        client.post("/simulations", json=SIMULATION_BODY)

    ((_, report_request_id, context),) = submissions.reports
    ((_, simulation_request_id, _),) = submissions.simulations
    assert report_request_id == "request-1"
    assert context["observability_id"] == OBSERVABILITY_ID
    assert simulation_request_id not in {"", OBSERVABILITY_ID}


@pytest.mark.parametrize(
    ("path", "body", "record"),
    [
        ("/reports", REPORT_BODY, "report"),
        ("/simulations", SIMULATION_BODY, "simulation"),
    ],
)
def test_a_create_whose_commit_is_unconfirmed_answers_a_500(
    client_for, capsys, path, body, record
) -> None:
    """A 500 tells the Public API the record may exist; no 503 may say it doesn't."""
    submissions = FakeEconomySubmissions(
        failure=EconomySubmissionOutcomeUnknownError(
            record, error_type="OperationalError"
        )
    )

    with client_for(submissions) as client:
        result = client.post(path, json=body)

    assert result.status_code == 500
    assert result.json() == {
        "code": "internal_error",
        "detail": (
            f"The service may have created the {record}, but could not confirm "
            "it was written."
        ),
    }
    printed = capsys.readouterr().out
    (logged,) = _emitted_records(printed, "economy_record_outcome_unknown")
    assert logged["attributes"]["record"] == record
    assert logged["attributes"]["error_type"] == "OperationalError"
    # One event for one outcome.
    assert _emitted_records(printed, "economy_records_outcome_unknown") == []


@pytest.mark.parametrize(("path", "body"), ROUTES)
@pytest.mark.parametrize(
    ("request_id", "kept"),
    [
        ("r" * 128, True),
        ("r" * 129, False),
        ("r" * 256, False),
        ("has spaces in it", False),
    ],
    ids=["128-kept", "129-replaced", "256-replaced", "spaces-replaced"],
)
def test_a_request_id_the_call_cannot_carry_is_replaced(
    client_for, path, body, request_id, kept
) -> None:
    """The call's context takes 1 to 128 of the observability runtime's characters."""
    submissions = FakeEconomySubmissions()

    with client_for(submissions) as client:
        result = client.post(path, json=body, headers={REQUEST_ID_HEADER: request_id})

    assert result.status_code == 201
    ((_, call_request_id, _),) = submissions.reports or submissions.simulations
    if kept:
        assert call_request_id == request_id
    else:
        assert str(UUID(call_request_id)) == call_request_id


@pytest.mark.parametrize("state", [{"request_id": ""}, {"request_id": None}, {}])
def test_a_request_without_a_usable_id_gets_a_new_one(state) -> None:
    request = SimpleNamespace(state=SimpleNamespace(**state))

    call_request_id = call_request_id_for(request)

    assert str(UUID(call_request_id)) == call_request_id
