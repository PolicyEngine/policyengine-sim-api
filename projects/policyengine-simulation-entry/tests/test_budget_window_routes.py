"""POST /reports with a budget window: the route sends it to the window service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from conftest import FakeBackend, make_settings
from economy_service_fakes import REPORT_BODY
from fastapi.testclient import TestClient
from policyengine_simulation_entry import app as app_module
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.budget_window_api_models import (
    BudgetWindowReportCreated,
    BudgetWindowYearCreated,
)
from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionUnavailableError,
)
from policyengine_simulation_persistence import ReportRunStatus
from test_budget_window_requests import WINDOW_BODY

REPORT_ID = UUID("00000000-0000-4000-8000-0000000000e1")
WINDOW_RUN_ID = UUID("00000000-0000-4000-8000-0000000000e2")
YEAR_RUN_IDS = [UUID(f"00000000-0000-4000-8000-0000000001{digit}0") for digit in "12"]
CREATED_WINDOW = BudgetWindowReportCreated(
    report_id=REPORT_ID,
    report_run_id=YEAR_RUN_IDS[0],
    budget_window_run_id=WINDOW_RUN_ID,
    status=ReportRunStatus.PENDING,
    years=[
        BudgetWindowYearCreated(
            year=2026 + index,
            report_run_id=run_id,
            baseline_simulation_id=UUID(int=10 + index),
            baseline_simulation_run_id=UUID(int=20 + index),
            baseline_reused=False,
            reform_simulation_id=UUID(int=30 + index),
            reform_simulation_run_id=UUID(int=40 + index),
            reform_reused=False,
        )
        for index, run_id in enumerate(YEAR_RUN_IDS)
    ],
)


@dataclass
class FakeSubmissions:
    """Records which service method each report reached."""

    failure: Exception | None = None
    windows: list[Any] = field(default_factory=list)
    reports: list[Any] = field(default_factory=list)

    async def submit_budget_window(self, request, *, request_id, observability_context):
        self.windows.append(request)
        if self.failure is not None:
            raise self.failure
        return CREATED_WINDOW

    async def submit_report(self, request, *, request_id, observability_context):
        self.reports.append(request)
        msg = "an economy report reached the test"
        raise AssertionError(msg)


@pytest.fixture
def client_for(monkeypatch):
    def build(submissions: FakeSubmissions) -> TestClient:
        monkeypatch.setattr(
            app_module,
            "economy_submissions_for",
            lambda settings, database, **_: submissions,
        )
        return TestClient(
            create_app(
                settings=make_settings(),
                backend=FakeBackend(),
                auth_dependency=lambda: None,
            )
        )

    return build


def test_a_budget_window_answers_201_with_its_years(client_for) -> None:
    submissions = FakeSubmissions()

    with client_for(submissions) as client:
        result = client.post("/reports", json=WINDOW_BODY)

    assert result.status_code == 201
    assert result.json() == CREATED_WINDOW.model_dump(mode="json")
    ((request,),) = [submissions.windows]
    assert (request.year, request.budget_window_size) == (2026, 10)
    assert submissions.reports == []


def test_a_budget_window_sending_cliffs_is_refused_before_the_service(
    client_for,
) -> None:
    submissions = FakeSubmissions()

    with client_for(submissions) as client:
        result = client.post("/reports", json={**WINDOW_BODY, "include_cliffs": True})

    assert result.status_code == 422
    assert submissions.windows == []


def test_a_refused_first_year_answers_503_naming_the_report(client_for) -> None:
    submissions = FakeSubmissions(
        failure=EconomySubmissionUnavailableError(
            code="modal_call_refused",
            detail="Modal did not accept the report's call.",
            record_id=REPORT_ID,
        )
    )

    with client_for(submissions) as client:
        result = client.post("/reports", json=WINDOW_BODY)

    assert result.status_code == 503
    assert result.json()["report_id"] == str(REPORT_ID)
    assert result.json()["code"] == "modal_call_refused"


def test_an_economy_report_body_still_reaches_the_report_service(client_for) -> None:
    submissions = FakeSubmissions()

    with client_for(submissions) as client:
        client.post("/reports", json=REPORT_BODY)

    assert len(submissions.reports) == 1
    assert submissions.windows == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {**REPORT_BODY, "year": 1800},
            {"loc": ["body", "year"], "type": "greater_than_equal"},
        ),
        (
            {
                name: value
                for name, value in REPORT_BODY.items()
                if name != "report_type"
            },
            {"loc": ["body", "report_type"], "type": "missing"},
        ),
        (
            {**REPORT_BODY, "report_type": "not_a_report_type"},
            {"loc": ["body", "report_type"], "type": "literal_error"},
        ),
        (
            {**WINDOW_BODY, "budget_window_size": 0},
            {"loc": ["body", "budget_window_size"], "type": "greater_than_equal"},
        ),
        (
            {**WINDOW_BODY, "year": 2195},
            {"loc": ["body"], "type": "value_error"},
        ),
    ],
    ids=["economy-field", "no-type", "unknown-type", "window-field", "window-years"],
)
def test_a_refused_report_names_its_fields_as_an_economy_report_did(
    client_for, body, expected
) -> None:
    # Telling the two request types apart adds nothing to a refused field's
    # location: the Public API passes the locations to its callers.
    with client_for(FakeSubmissions()) as client:
        result = client.post("/reports", json=body)

    assert result.status_code == 422
    (error,) = result.json()["detail"]
    assert {"loc": error["loc"], "type": error["type"]} == expected


@pytest.mark.parametrize("report_type", [None, 5, ["economy"], "not_a_report_type"])
def test_an_unknown_report_type_is_refused_with_the_value_sent(
    client_for, report_type
) -> None:
    with client_for(FakeSubmissions()) as client:
        result = client.post(
            "/reports", json={**REPORT_BODY, "report_type": report_type}
        )

    assert result.status_code == 422
    assert result.json()["detail"] == [
        {
            "type": "literal_error",
            "loc": ["body", "report_type"],
            "msg": "Input should be 'economy', 'budget_window' or 'household'",
            "input": report_type,
            "ctx": {"expected": "'economy', 'budget_window' or 'household'"},
        }
    ]


def test_a_report_with_no_type_is_refused_as_an_economy_report_was(
    client_for,
) -> None:
    body = {name: value for name, value in REPORT_BODY.items() if name != "report_type"}

    with client_for(FakeSubmissions()) as client:
        result = client.post("/reports", json=body)

    assert result.json()["detail"] == [
        {
            "type": "missing",
            "loc": ["body", "report_type"],
            "msg": "Field required",
            "input": body,
        }
    ]
