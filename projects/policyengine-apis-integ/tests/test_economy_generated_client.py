"""Hermetic wire checks of the economy routes' generated client models.

The Public API sends its report and simulation requests through these
models, and reads each answer the routes declare.
"""

from uuid import UUID

import httpx
import pytest
from policyengine_api_simulation_client import AuthenticatedClient
from policyengine_api_simulation_client.api.default import (
    create_report,
    create_simulation,
    get_report,
    get_simulation,
    rerun_report,
    rerun_simulation,
)
from policyengine_api_simulation_client.models import (
    BudgetWindowReportCreated,
    EconomyError,
    EconomyReportCreated,
    EconomyReportRequest,
    EconomyReportState,
    EconomyRequestUnprocessable,
    EconomySimulationCreated,
    EconomySimulationRequest,
    EconomySimulationState,
    ReportRerunRequest,
    ReportRunStatus,
)

REPORT_ID = "00000000-0000-4000-8000-0000000000e1"
RUN_ID = "00000000-0000-4000-8000-0000000000e2"
SIDE_IDS = [f"00000000-0000-4000-8000-0000000000f{digit}" for digit in "1234"]
POLICY_ID = "00000000-0000-4000-8000-0000000000c5"
# The body the Public API sends (``simulation_reports.report_request``).
REPORT_BODY = {
    "country": "us",
    "report_type": "economy",
    "label": "A larger child tax credit",
    "description": None,
    "year": 2026,
    "region_code": "us",
    "dataset_name": None,
    "baseline": {"policy_id": None, "dynamic_id": None},
    "reform": {"policy_id": POLICY_ID, "dynamic_id": None},
    "include_cliffs": False,
}
SIMULATION_BODY = {
    "country": "uk",
    "simulation_type": "economy",
    "year": 2026,
    "region_code": "country/wales",
    "dataset_name": None,
    "policy_id": None,
    "dynamic_id": None,
}
CREATED_REPORT = {
    "report_id": REPORT_ID,
    "report_run_id": RUN_ID,
    "status": "pending",
    "baseline_simulation_id": SIDE_IDS[0],
    "baseline_simulation_run_id": SIDE_IDS[1],
    "baseline_reused": True,
    "reform_simulation_id": SIDE_IDS[2],
    "reform_simulation_run_id": SIDE_IDS[3],
    "reform_reused": False,
}


def test_a_report_request_keeps_omitted_spm_settings_omitted():
    """Omitting spm uses the bundle's defaults; null is not a selection."""
    omitted = EconomyReportRequest.from_dict(REPORT_BODY).to_dict()
    given = EconomyReportRequest.from_dict(
        {**REPORT_BODY, "spm": {"geography_kind": "national"}}
    ).to_dict()

    assert "spm" not in omitted
    assert omitted["reform"] == {"policy_id": POLICY_ID, "dynamic_id": None}
    assert given["spm"] == {"geography_kind": "national"}


def _client_answering(status_code: int, body: dict) -> AuthenticatedClient:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, json=body)
    )
    client = AuthenticatedClient(base_url="https://simulation.example", token="test")
    client.set_httpx_client(
        httpx.Client(base_url="https://simulation.example", transport=transport)
    )
    return client


def _create_report(status_code: int, body: dict):
    return create_report.sync_detailed(
        client=_client_answering(status_code, body),
        body=EconomyReportRequest.from_dict(REPORT_BODY),
    )


def test_a_created_report_is_read_with_its_runs():
    response = _create_report(201, CREATED_REPORT)

    assert isinstance(response.parsed, EconomyReportCreated)
    assert response.parsed.status is ReportRunStatus.PENDING
    assert response.parsed.to_dict() == CREATED_REPORT


def test_a_created_simulation_is_read_with_its_run():
    created = {
        "simulation_id": REPORT_ID,
        "simulation_run_id": RUN_ID,
        "status": "pending",
    }

    response = create_simulation.sync_detailed(
        client=_client_answering(201, created),
        body=EconomySimulationRequest.from_dict(SIMULATION_BODY),
    )

    assert isinstance(response.parsed, EconomySimulationCreated)
    assert response.parsed.to_dict() == created


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (400, {"code": "unknown_region", "detail": "no region us"}),
        (
            503,
            {
                "code": "modal_call_refused",
                "detail": "Modal did not accept the report's call.",
                "report_id": REPORT_ID,
            },
        ),
        (500, {"code": "internal_error", "detail": "The service could not answer."}),
    ],
)
def test_an_error_is_read_with_its_code_and_any_record(status_code, body):
    response = _create_report(status_code, body)

    assert isinstance(response.parsed, EconomyError)
    assert response.parsed.to_dict() == body


def test_stored_values_that_cannot_be_calculated_are_read_with_their_code():
    body = {
        "detail": [
            {
                "loc": ["body", "policy_id", "gov.a"],
                "msg": "no such parameter",
                "type": "missing_parameter",
            }
        ],
        "code": "missing_parameters",
    }

    response = _create_report(422, body)

    assert isinstance(response.parsed, EconomyRequestUnprocessable)
    assert response.parsed.to_dict() == body


def test_a_report_and_a_simulation_are_read_with_their_newest_runs():
    report = {
        "report_id": REPORT_ID,
        "country": "us",
        "baseline_simulation_id": SIDE_IDS[0],
        "reform_simulation_id": SIDE_IDS[2],
        "run": {
            "run_id": RUN_ID,
            "status": "succeeded",
            "country_package_version": "2.2.1",
            "policyengine_version": "6.17.1",
            "error_message": None,
            "created_at": "2026-11-12T15:00:00Z",
            "started_at": "2026-11-12T15:00:05Z",
            "completed_at": "2026-11-12T15:04:00Z",
            "baseline_simulation_run_id": SIDE_IDS[1],
            "reform_simulation_run_id": SIDE_IDS[3],
        },
    }
    simulation = {"simulation_id": REPORT_ID, "run": None}

    report_response = get_report.sync_detailed(
        UUID(REPORT_ID), client=_client_answering(200, report)
    )
    simulation_response = get_simulation.sync_detailed(
        UUID(REPORT_ID), client=_client_answering(200, simulation)
    )

    assert isinstance(report_response.parsed, EconomyReportState)
    run = report_response.parsed.run
    assert run is not None
    assert run.status is ReportRunStatus.SUCCEEDED
    assert isinstance(simulation_response.parsed, EconomySimulationState)
    assert simulation_response.parsed.to_dict() == simulation


def test_a_report_rerun_is_sent_with_force_and_read_with_its_new_run():
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(201, json=CREATED_REPORT)

    client = AuthenticatedClient(base_url="https://simulation.example", token="test")
    client.set_httpx_client(
        httpx.Client(
            base_url="https://simulation.example",
            transport=httpx.MockTransport(answer),
        )
    )

    response = rerun_report.sync_detailed(
        UUID(REPORT_ID), client=client, body=ReportRerunRequest(force=True)
    )

    (request,) = sent
    assert request.url.path == f"/reports/{REPORT_ID}/runs"
    assert request.read() == b'{"force":true}'
    assert isinstance(response.parsed, EconomyReportCreated)
    assert response.parsed.to_dict() == CREATED_REPORT


def test_a_simulation_rerun_is_read_with_its_new_run():
    created = {
        "simulation_id": REPORT_ID,
        "simulation_run_id": RUN_ID,
        "status": "pending",
    }

    response = rerun_simulation.sync_detailed(
        UUID(REPORT_ID), client=_client_answering(201, created)
    )

    assert isinstance(response.parsed, EconomySimulationCreated)
    assert response.parsed.to_dict() == created


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (404, {"code": "unknown_report", "detail": f"There is no report {REPORT_ID}."}),
        (
            409,
            {
                "code": "run_in_progress",
                "detail": "The report has a run pending or running.",
                "run_id": RUN_ID,
            },
        ),
        (409, {"code": "rerun_not_supported", "detail": "Not yet."}),
        (
            503,
            {
                "code": "modal_call_refused",
                "detail": "Modal did not accept the call of the report's new run.",
                "report_id": REPORT_ID,
                "run_id": RUN_ID,
            },
        ),
    ],
)
def test_a_refused_rerun_is_read_with_its_code_and_any_run(status_code, body):
    response = rerun_report.sync_detailed(
        UUID(REPORT_ID),
        client=_client_answering(status_code, body),
        body=ReportRerunRequest(),
    )

    assert isinstance(response.parsed, EconomyError)
    assert response.parsed.to_dict() == body


def test_a_budget_window_rerun_is_read_with_its_new_window_run():
    window = {
        "report_id": REPORT_ID,
        "report_run_id": RUN_ID,
        "budget_window_run_id": SIDE_IDS[0],
        "status": "pending",
        "years": [
            {
                "year": 2026,
                "report_run_id": RUN_ID,
                "baseline_simulation_id": SIDE_IDS[1],
                "baseline_simulation_run_id": SIDE_IDS[2],
                "baseline_reused": False,
                "reform_simulation_id": SIDE_IDS[3],
                "reform_simulation_run_id": SIDE_IDS[0],
                "reform_reused": True,
            }
        ],
    }

    response = rerun_report.sync_detailed(
        UUID(REPORT_ID),
        client=_client_answering(201, window),
        body=ReportRerunRequest(),
    )

    assert isinstance(response.parsed, BudgetWindowReportCreated)
    assert response.parsed.to_dict() == window
