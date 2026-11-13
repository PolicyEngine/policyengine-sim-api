"""The re-run routes' answers, with the re-run service faked."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NoReturn
from uuid import UUID

import pytest
from conftest import FakeBackend, make_settings
from fastapi.testclient import TestClient
from policyengine_simulation_entry import app as app_module
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.budget_window_api_models import (
    BudgetWindowReportCreated,
    BudgetWindowYearCreated,
)
from policyengine_simulation_entry.economy_api_models import (
    EconomyReportCreated,
    EconomySimulationCreated,
    ReportRerunRequest,
)
from policyengine_simulation_entry.economy_submission_errors import (
    EconomyRecordNotFoundError,
    EconomyRerunConflictError,
    EconomySubmissionOutcomeUnknownError,
    EconomySubmissionRefusedError,
    EconomySubmissionUnavailableError,
    EconomySubmissionUnprocessableError,
    UnprocessableReason,
)
from policyengine_simulation_entry.household_report_api_models import (
    HouseholdReportCreated,
)
from policyengine_simulation_observability.identifiers import OBSERVABILITY_ID_HEADER
from policyengine_simulation_persistence import ReportRunStatus, SimulationStatus

OBSERVABILITY_ID = "6e1f3d4c-0a5b-4c39-9d2e-7f8a9b0c1d2e"
RECORD_ID = UUID("00000000-0000-4000-8000-0000000000e1")
RUN_ID = UUID("00000000-0000-4000-8000-0000000000e2")
SIDE_IDS = [UUID(f"00000000-0000-4000-8000-0000000000f{digit}") for digit in "1234"]
REPORT_RUNS = f"/reports/{RECORD_ID}/runs"
SIMULATION_RUNS = f"/simulations/{RECORD_ID}/runs"
RERUN_REPORT = EconomyReportCreated(
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
RERUN_SIMULATION = EconomySimulationCreated(
    simulation_id=RECORD_ID, simulation_run_id=RUN_ID, status=SimulationStatus.PENDING
)


def _raise(failure: Exception) -> NoReturn:
    raise failure


@dataclass
class FakeEconomyReruns:
    """Records each re-run, and answers as told."""

    failure: Exception | None = None
    report_answer: (
        EconomyReportCreated | BudgetWindowReportCreated | HouseholdReportCreated
    ) = RERUN_REPORT
    report_reruns: list[tuple[UUID, ReportRerunRequest, str, Any]] = field(
        default_factory=list
    )
    simulation_reruns: list[tuple[UUID, str, Any]] = field(default_factory=list)

    async def rerun_report(
        self,
        report_id: UUID,
        request: ReportRerunRequest,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomyReportCreated | BudgetWindowReportCreated | HouseholdReportCreated:
        self.report_reruns.append(
            (report_id, request, request_id, observability_context)
        )
        if self.failure is not None:
            _raise(self.failure)
        return self.report_answer

    async def rerun_simulation(
        self,
        simulation_id: UUID,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomySimulationCreated:
        self.simulation_reruns.append(
            (simulation_id, request_id, observability_context)
        )
        if self.failure is not None:
            _raise(self.failure)
        return RERUN_SIMULATION


@pytest.fixture
def client_for(monkeypatch):
    """Return a function building a client whose app uses ``reruns``.

    None leaves the app as an unconfigured service builds it.
    """

    def build(reruns: FakeEconomyReruns | None) -> TestClient:
        if reruns is not None:
            monkeypatch.setattr(
                app_module,
                "economy_submissions_for",
                lambda settings, database, **_: reruns,
            )
        return TestClient(
            create_app(
                settings=make_settings(),
                backend=FakeBackend(),
                auth_dependency=lambda: None,
            )
        )

    return build


# Each re-run route, with a body it accepts.
ROUTES = [(REPORT_RUNS, {}), (SIMULATION_RUNS, None)]


def test_a_report_rerun_answers_201_with_its_new_run(client_for) -> None:
    reruns = FakeEconomyReruns()

    with client_for(reruns) as client:
        result = client.post(
            REPORT_RUNS,
            json={"force": True},
            headers={OBSERVABILITY_ID_HEADER: OBSERVABILITY_ID},
        )

    assert result.status_code == 201
    assert result.json() == RERUN_REPORT.model_dump(mode="json")
    ((report_id, request, request_id, context),) = reruns.report_reruns
    assert (report_id, request) == (RECORD_ID, ReportRerunRequest(force=True))
    assert request_id != OBSERVABILITY_ID
    assert context["observability_id"] == OBSERVABILITY_ID


def test_a_budget_window_rerun_answers_201_with_its_new_window_run(client_for) -> None:
    window = BudgetWindowReportCreated(
        report_id=RECORD_ID,
        report_run_id=RUN_ID,
        budget_window_run_id=SIDE_IDS[0],
        status=ReportRunStatus.PENDING,
        years=[
            BudgetWindowYearCreated(
                year=2026,
                report_run_id=RUN_ID,
                baseline_simulation_id=SIDE_IDS[1],
                baseline_simulation_run_id=SIDE_IDS[2],
                baseline_reused=False,
                reform_simulation_id=SIDE_IDS[3],
                reform_simulation_run_id=SIDE_IDS[0],
                reform_reused=False,
            )
        ],
    )
    reruns = FakeEconomyReruns(report_answer=window)

    with client_for(reruns) as client:
        result = client.post(REPORT_RUNS, json={})

    assert result.status_code == 201
    assert result.json() == window.model_dump(mode="json")


def test_a_household_report_rerun_answers_201_with_its_new_run(client_for) -> None:
    household = HouseholdReportCreated(
        report_id=RECORD_ID,
        report_run_id=RUN_ID,
        status=ReportRunStatus.PENDING,
        year=2025,
        baseline_simulation_id=SIDE_IDS[0],
        baseline_simulation_run_id=SIDE_IDS[1],
        reform_simulation_id=None,
        reform_simulation_run_id=None,
    )
    reruns = FakeEconomyReruns(report_answer=household)

    with client_for(reruns) as client:
        result = client.post(REPORT_RUNS, json={})

    assert result.status_code == 201
    assert result.json() == household.model_dump(mode="json")


def test_a_report_rerun_without_force_reuses_runs(client_for) -> None:
    reruns = FakeEconomyReruns()

    with client_for(reruns) as client:
        result = client.post(REPORT_RUNS, json={})

    assert result.status_code == 201
    assert reruns.report_reruns[0][1] == ReportRerunRequest(force=False)


def test_a_simulation_rerun_answers_201_with_its_new_run(client_for) -> None:
    reruns = FakeEconomyReruns()

    with client_for(reruns) as client:
        result = client.post(SIMULATION_RUNS)

    assert result.status_code == 201
    assert result.json() == {
        "simulation_id": str(RECORD_ID),
        "simulation_run_id": str(RUN_ID),
        "status": "pending",
    }
    ((simulation_id, _, context),) = reruns.simulation_reruns
    assert simulation_id == RECORD_ID
    assert context is not None


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (REPORT_RUNS, {"force": "yes"}),
        (REPORT_RUNS, {"force": None}),
        (REPORT_RUNS, {"policyengine_version": "6.17.1"}),
        ("/reports/not-a-uuid/runs", {}),
        ("/simulations/not-a-uuid/runs", None),
    ],
)
def test_a_malformed_rerun_is_refused_with_its_reasons(client_for, path, body):
    reruns = FakeEconomyReruns()

    with client_for(reruns) as client:
        result = client.post(path, json=body)

    assert result.status_code == 422
    assert all({"loc", "msg"} <= set(reason) for reason in result.json()["detail"])
    assert (reruns.report_reruns, reruns.simulation_reruns) == ([], [])


@pytest.mark.parametrize(
    ("path", "body", "record"),
    [(REPORT_RUNS, {}, "report"), (SIMULATION_RUNS, None, "simulation")],
)
def test_an_unknown_record_answers_404(client_for, path, body, record) -> None:
    reruns = FakeEconomyReruns(failure=EconomyRecordNotFoundError(record))

    with client_for(reruns) as client:
        result = client.post(path, json=body)

    assert result.status_code == 404
    assert result.json() == {
        "code": f"unknown_{record}",
        "detail": f"There is no {record} {RECORD_ID}.",
    }


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_a_run_under_way_answers_409_naming_it(client_for, path, body) -> None:
    reruns = FakeEconomyReruns(
        failure=EconomyRerunConflictError(
            code="run_in_progress", detail="a run is under way", run_id=RUN_ID
        )
    )

    with client_for(reruns) as client:
        result = client.post(path, json=body)

    assert result.status_code == 409
    assert result.json() == {
        "code": "run_in_progress",
        "detail": "a run is under way",
        "run_id": str(RUN_ID),
    }


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_a_record_the_service_cannot_rerun_answers_409(client_for, path, body) -> None:
    reruns = FakeEconomyReruns(
        failure=EconomyRerunConflictError(
            code="rerun_not_supported", detail="not budget windows yet"
        )
    )

    with client_for(reruns) as client:
        result = client.post(path, json=body)

    assert result.status_code == 409
    assert result.json() == {
        "code": "rerun_not_supported",
        "detail": "not budget windows yet",
    }


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_a_refused_rerun_answers_400_or_422(client_for, path, body) -> None:
    refused = FakeEconomyReruns(
        failure=EconomySubmissionRefusedError(
            code="unknown_region", detail="no region us, which the report names"
        )
    )
    unprocessable = FakeEconomyReruns(
        failure=EconomySubmissionUnprocessableError(
            code="missing_parameters",
            reasons=(
                UnprocessableReason(
                    loc=("report", "reform", "policy_id", "gov.a"),
                    msg="no such parameter",
                    type="missing_parameter",
                ),
            ),
        )
    )

    with client_for(refused) as client:
        refusal = client.post(path, json=body)
    with client_for(unprocessable) as client:
        unprocessable_answer = client.post(path, json=body)

    assert (refusal.status_code, refusal.json()["code"]) == (400, "unknown_region")
    assert unprocessable_answer.status_code == 422
    assert unprocessable_answer.json() == {
        "detail": [
            {
                "loc": ["report", "reform", "policy_id", "gov.a"],
                "msg": "no such parameter",
                "type": "missing_parameter",
            }
        ],
        "code": "missing_parameters",
    }


@pytest.mark.parametrize(
    ("path", "body", "record"),
    [(REPORT_RUNS, {}, "report"), (SIMULATION_RUNS, None, "simulation")],
)
def test_a_rerun_modal_refused_answers_503_naming_the_record_and_its_run(
    client_for, path, body, record
) -> None:
    reruns = FakeEconomyReruns(
        failure=EconomySubmissionUnavailableError(
            code="modal_call_refused",
            detail="Modal refused it",
            record_id=RECORD_ID,
            run_id=RUN_ID,
        )
    )

    with client_for(reruns) as client:
        result = client.post(path, json=body)

    assert result.status_code == 503
    assert result.headers["Retry-After"] == "10"
    assert result.json() == {
        "code": "modal_call_refused",
        "detail": "Modal refused it",
        f"{record}_id": str(RECORD_ID),
        "run_id": str(RUN_ID),
    }


@pytest.mark.parametrize(
    ("path", "body", "record"),
    [(REPORT_RUNS, {}, "report"), (SIMULATION_RUNS, None, "simulation")],
)
def test_a_rerun_whose_commit_is_unconfirmed_answers_500(
    client_for, path, body, record
) -> None:
    reruns = FakeEconomyReruns(
        failure=EconomySubmissionOutcomeUnknownError(
            record, error_type="OperationalError", rerun=True
        )
    )

    with client_for(reruns) as client:
        result = client.post(path, json=body)

    assert result.status_code == 500
    assert result.json() == {
        "code": "internal_error",
        "detail": (
            f"The service may have created the {record}'s new run, but could not "
            "confirm it was written."
        ),
    }


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_an_unexpected_failure_answers_the_json_500(client_for, path, body) -> None:
    reruns = FakeEconomyReruns(failure=RuntimeError("secret detail"))

    with client_for(reruns) as client:
        result = client.post(path, json=body)

    assert result.status_code == 500
    assert result.json() == {
        "code": "internal_error",
        "detail": "The service could not answer the request.",
    }


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_an_unconfigured_service_answers_503(client_for, path, body) -> None:
    with client_for(None) as client:
        result = client.post(path, json=body)

    assert result.status_code == 503
    assert result.json()["code"] == "not_configured"


def test_a_report_rerun_takes_no_data_source(client_for) -> None:
    """A re-run keeps the stored report's dataset, whatever it was created with."""
    reruns = FakeEconomyReruns()

    with client_for(reruns) as client:
        result = client.post(REPORT_RUNS, json={"data_source": "local_area_dataset"})

    assert result.status_code == 422
    (reason,) = result.json()["detail"]
    assert (reason["loc"][-1], reason["type"]) == ("data_source", "extra_forbidden")
    assert reruns.report_reruns == []
