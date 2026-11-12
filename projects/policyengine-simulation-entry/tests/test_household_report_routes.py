"""``POST /reports`` for household reports, with the submission services faked."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NoReturn
from uuid import UUID

import pytest
from conftest import FakeBackend, make_settings
from economy_service_fakes import REPORT_BODY
from fastapi.testclient import TestClient
from household_report_fakes import HOUSEHOLD_REPORT_BODY, POLICY_ID
from policyengine_simulation_entry import app as app_module
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionOutcomeUnknownError,
    EconomySubmissionRefusedError,
    EconomySubmissionUnavailableError,
    EconomySubmissionUnprocessableError,
    UnprocessableReason,
)
from policyengine_simulation_entry.household_report_api_models import (
    HouseholdReportCreated,
    HouseholdReportRequest,
)
from policyengine_simulation_observability.identifiers import OBSERVABILITY_ID_HEADER
from policyengine_simulation_persistence import ReportRunStatus
from test_economy_routes import CREATED_REPORT, FakeEconomySubmissions

OBSERVABILITY_ID = "6e1f3d4c-0a5b-4c39-9d2e-7f8a9b0c1d2e"
REPORT_ID = UUID("00000000-0000-4000-8000-0000000000e1")
CREATED_HOUSEHOLD_REPORT = HouseholdReportCreated(
    report_id=REPORT_ID,
    report_run_id=UUID("00000000-0000-4000-8000-0000000000e2"),
    status=ReportRunStatus.PENDING,
    year=2026,
    baseline_simulation_id=UUID("00000000-0000-4000-8000-0000000000f1"),
    baseline_simulation_run_id=UUID("00000000-0000-4000-8000-0000000000f2"),
    reform_simulation_id=UUID("00000000-0000-4000-8000-0000000000f3"),
    reform_simulation_run_id=UUID("00000000-0000-4000-8000-0000000000f4"),
)


def _raise(failure: Exception) -> NoReturn:
    raise failure


@dataclass
class FakeHouseholdReportSubmissions:
    """Records each household report submission, and answers as told."""

    failure: Exception | None = None
    reports: list[tuple[HouseholdReportRequest, Any]] = field(default_factory=list)

    async def submit_report(
        self,
        request: HouseholdReportRequest,
        *,
        observability_context: dict[str, Any] | None,
    ) -> HouseholdReportCreated:
        self.reports.append((request, observability_context))
        if self.failure is not None:
            _raise(self.failure)
        return CREATED_HOUSEHOLD_REPORT


@pytest.fixture
def client_for(monkeypatch):
    """Return a function building a client whose app uses these services.

    A household service of None leaves household reports unconfigured.
    """

    def build(
        household_reports: FakeHouseholdReportSubmissions | None,
        economy: FakeEconomySubmissions | None = None,
    ) -> TestClient:
        monkeypatch.setattr(
            app_module,
            "economy_submissions_for",
            lambda settings, database: economy or FakeEconomySubmissions(),
        )
        monkeypatch.setattr(
            app_module,
            "household_reports_for",
            lambda settings, database: household_reports,
        )
        return TestClient(
            create_app(
                settings=make_settings(),
                backend=FakeBackend(),
                auth_dependency=lambda: None,
            )
        )

    return build


def test_a_household_report_answers_201_with_its_records_and_runs(client_for) -> None:
    household_reports = FakeHouseholdReportSubmissions()
    economy = FakeEconomySubmissions()

    with client_for(household_reports, economy) as client:
        result = client.post(
            "/reports",
            json=HOUSEHOLD_REPORT_BODY,
            headers={OBSERVABILITY_ID_HEADER: OBSERVABILITY_ID},
        )

    assert result.status_code == 201
    assert result.json() == CREATED_HOUSEHOLD_REPORT.model_dump(mode="json")
    ((request, context),) = household_reports.reports
    assert request.household_id == UUID(HOUSEHOLD_REPORT_BODY["household_id"])
    assert request.reform is not None
    assert request.reform.policy_id == POLICY_ID
    assert request.options() == {"add_missing_variables": True}
    assert context["observability_id"] == OBSERVABILITY_ID
    assert economy.reports == []


def test_an_economy_report_still_reaches_the_economy_service(client_for) -> None:
    household_reports = FakeHouseholdReportSubmissions()
    economy = FakeEconomySubmissions()

    with client_for(household_reports, economy) as client:
        result = client.post("/reports", json=REPORT_BODY)

    assert result.status_code == 201
    assert result.json() == CREATED_REPORT.model_dump(mode="json")
    assert len(economy.reports) == 1
    assert household_reports.reports == []


def test_a_report_of_the_baseline_alone_may_leave_out_its_reform(client_for) -> None:
    household_reports = FakeHouseholdReportSubmissions()
    body = {
        key: value for key, value in HOUSEHOLD_REPORT_BODY.items() if key != "reform"
    }

    with client_for(household_reports) as client:
        result = client.post("/reports", json=body)

    assert result.status_code == 201
    ((request, _),) = household_reports.reports
    assert request.reform is None
    assert request.add_missing_variables is True


def test_household_reports_answer_503_until_they_are_configured(client_for) -> None:
    with client_for(None) as client:
        result = client.post("/reports", json=HOUSEHOLD_REPORT_BODY)

    assert result.status_code == 503
    assert result.headers["Retry-After"] == "10"
    assert result.json() == {
        "code": "not_configured",
        "detail": "Household reports are not configured.",
    }


@pytest.mark.parametrize(
    ("body", "location", "error_type"),
    [
        (
            {k: v for k, v in HOUSEHOLD_REPORT_BODY.items() if k != "household_id"},
            ["body", "household_id"],
            "missing",
        ),
        (
            {
                **HOUSEHOLD_REPORT_BODY,
                "baseline": {"policy_id": None, "dynamic_id": None},
            },
            ["body", "baseline", "dynamic_id"],
            "extra_forbidden",
        ),
        (
            {**HOUSEHOLD_REPORT_BODY, "reform": {"policy_id": "not-a-uuid"}},
            ["body", "reform", "policy_id"],
            "uuid_parsing",
        ),
        (
            {**HOUSEHOLD_REPORT_BODY, "add_missing_variables": "yes"},
            ["body", "add_missing_variables"],
            "bool_type",
        ),
        (
            {**HOUSEHOLD_REPORT_BODY, "year": 2026},
            ["body", "year"],
            "extra_forbidden",
        ),
        (
            {k: v for k, v in HOUSEHOLD_REPORT_BODY.items() if k != "report_type"},
            ["body", "report_type"],
            "missing",
        ),
    ],
    ids=[
        "no-household",
        "dynamic",
        "malformed-policy",
        "option-not-a-bool",
        "year",
        "no-report-type",
    ],
)
def test_a_refused_household_report_names_its_fields_without_its_type(
    client_for, body, location, error_type
) -> None:
    household_reports = FakeHouseholdReportSubmissions()

    with client_for(household_reports) as client:
        result = client.post("/reports", json=body)

    assert result.status_code == 422
    reasons = result.json()["detail"]
    assert [reason["loc"] for reason in reasons] == [location]
    assert [reason["type"] for reason in reasons] == [error_type]
    assert household_reports.reports == []


def test_an_unknown_report_type_is_refused_with_the_types_there_are(
    client_for,
) -> None:
    with client_for(FakeHouseholdReportSubmissions()) as client:
        result = client.post(
            "/reports",
            json={**HOUSEHOLD_REPORT_BODY, "report_type": "not_a_report_type"},
        )

    assert result.status_code == 422
    ((reason),) = result.json()["detail"]
    assert reason["loc"] == ["body", "report_type"]
    assert reason["type"] == "literal_error"
    assert reason["msg"] == "Input should be 'economy', 'budget_window' or 'household'"
    assert reason["input"] == "not_a_report_type"


@pytest.mark.parametrize(
    ("failure", "status_code", "answer"),
    [
        (
            EconomySubmissionRefusedError(
                code="unknown_household",
                detail="us has no stored household the request names.",
            ),
            400,
            {
                "code": "unknown_household",
                "detail": "us has no stored household the request names.",
            },
        ),
        (
            EconomySubmissionUnprocessableError(
                code="household_not_calculable",
                reasons=(
                    UnprocessableReason(
                        loc=("body", "household_id"),
                        msg="the household has no default year to calculate",
                        type="household_without_default_year",
                    ),
                ),
            ),
            422,
            {
                "detail": [
                    {
                        "loc": ["body", "household_id"],
                        "msg": "the household has no default year to calculate",
                        "type": "household_without_default_year",
                    }
                ],
                "code": "household_not_calculable",
            },
        ),
        (
            EconomySubmissionUnavailableError(
                code="modal_call_refused",
                detail="Modal did not accept the report's call.",
                record_id=REPORT_ID,
            ),
            503,
            {
                "code": "modal_call_refused",
                "detail": "Modal did not accept the report's call.",
                "report_id": str(REPORT_ID),
            },
        ),
        (
            EconomySubmissionOutcomeUnknownError(
                "report", error_type="OperationalError"
            ),
            500,
            {
                "code": "internal_error",
                "detail": (
                    "The service may have created the report, but could not "
                    "confirm it was written."
                ),
            },
        ),
        (
            RuntimeError("the household function's request was built wrong"),
            500,
            {
                "code": "internal_error",
                "detail": "The service could not answer the request.",
            },
        ),
    ],
    ids=["refused", "unprocessable", "call-refused", "outcome-unknown", "unexpected"],
)
def test_a_household_report_the_service_refuses_answers_with_its_code(
    client_for, failure, status_code, answer
) -> None:
    with client_for(FakeHouseholdReportSubmissions(failure=failure)) as client:
        result = client.post("/reports", json=HOUSEHOLD_REPORT_BODY)

    assert result.status_code == status_code
    assert result.json() == answer
