"""Hermetic wire checks of a budget-window report through the generated client.

The Public API sends a budget window to ``POST /reports`` with
``report_type`` ``budget_window``, and reads the 201 naming the report, the
window run and each year's runs.
"""

import httpx
from policyengine_api_simulation_client import AuthenticatedClient
from policyengine_api_simulation_client.api.default import create_report
from policyengine_api_simulation_client.models import (
    BudgetWindowReportCreated,
    BudgetWindowReportRequest,
    EconomyError,
    ReportRunStatus,
)

REPORT_ID = "00000000-0000-4000-8000-0000000000e1"
WINDOW_RUN_ID = "00000000-0000-4000-8000-0000000000e2"
POLICY_ID = "00000000-0000-4000-8000-0000000000c5"
WINDOW_BODY = {
    "country": "us",
    "report_type": "budget_window",
    "label": "Ten-year budget window",
    "description": None,
    "year": 2026,
    "budget_window_size": 2,
    "region_code": "us",
    "baseline": {"policy_id": None, "dynamic_id": None},
    "reform": {"policy_id": POLICY_ID, "dynamic_id": None},
}
YEARS = [
    {
        "year": 2026 + index,
        "report_run_id": f"00000000-0000-4000-8000-00000000010{index}",
        "baseline_simulation_id": f"00000000-0000-4000-8000-00000000020{index}",
        "baseline_simulation_run_id": f"00000000-0000-4000-8000-00000000030{index}",
        "baseline_reused": index == 1,
        "reform_simulation_id": f"00000000-0000-4000-8000-00000000040{index}",
        "reform_simulation_run_id": f"00000000-0000-4000-8000-00000000050{index}",
        "reform_reused": False,
    }
    for index in range(2)
]
CREATED_WINDOW = {
    "report_id": REPORT_ID,
    "report_run_id": YEARS[0]["report_run_id"],
    "budget_window_run_id": WINDOW_RUN_ID,
    "status": "pending",
    "years": YEARS,
}


def _create_window(status_code: int, body: dict):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, json=body)
    )
    client = AuthenticatedClient(base_url="https://simulation.example", token="test")
    client.set_httpx_client(
        httpx.Client(base_url="https://simulation.example", transport=transport)
    )
    return create_report.sync_detailed(
        client=client, body=BudgetWindowReportRequest.from_dict(WINDOW_BODY)
    )


def test_a_budget_window_request_is_sent_as_given():
    sent = BudgetWindowReportRequest.from_dict(WINDOW_BODY).to_dict()

    assert sent == WINDOW_BODY


def test_a_created_budget_window_is_read_with_its_years():
    response = _create_window(201, CREATED_WINDOW)

    assert isinstance(response.parsed, BudgetWindowReportCreated)
    assert response.parsed.status is ReportRunStatus.PENDING
    assert response.parsed.to_dict() == CREATED_WINDOW


def test_a_refused_first_year_is_read_with_its_report():
    response = _create_window(
        503,
        {
            "code": "modal_call_refused",
            "detail": "Modal did not accept the report's call.",
            "report_id": REPORT_ID,
        },
    )

    assert isinstance(response.parsed, EconomyError)
    assert str(response.parsed.report_id) == REPORT_ID
