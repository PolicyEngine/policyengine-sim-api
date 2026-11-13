"""Hermetic wire checks of a household report through the generated client.

The Public API sends a household report to ``POST /reports`` with
``report_type`` ``household``, and reads the 201 naming the report, its run,
its year and each side's simulation and simulation run, or the error
answers economy reports share. A household report's re-run
(``POST /reports/{report_id}/runs``) answers with the same 201.
"""

from uuid import UUID

import httpx
from policyengine_api_simulation_client import AuthenticatedClient
from policyengine_api_simulation_client.api.default import create_report, rerun_report
from policyengine_api_simulation_client.models import (
    EconomyError,
    EconomyReportCreated,
    EconomyRequestUnprocessable,
    HouseholdReportCreated,
    HouseholdReportRequest,
    ReportRerunRequest,
    ReportRunStatus,
)

REPORT_ID = "00000000-0000-4000-8000-0000000000e1"
RUN_ID = "00000000-0000-4000-8000-0000000000e2"
SIDE_IDS = [f"00000000-0000-4000-8000-0000000000f{digit}" for digit in "1234"]
HOUSEHOLD_ID = "00000000-0000-4000-8000-0000000000bb"
POLICY_ID = "00000000-0000-4000-8000-0000000000c5"
HOUSEHOLD_REPORT_BODY = {
    "country": "us",
    "report_type": "household",
    "label": "A larger child tax credit for one family",
    "description": None,
    "household_id": HOUSEHOLD_ID,
    "baseline": {"policy_id": None},
    "reform": {"policy_id": POLICY_ID},
}
CREATED_HOUSEHOLD_REPORT = {
    "report_id": REPORT_ID,
    "report_run_id": RUN_ID,
    "status": "pending",
    "year": 2026,
    "baseline_simulation_id": SIDE_IDS[0],
    "baseline_simulation_run_id": SIDE_IDS[1],
    "reform_simulation_id": SIDE_IDS[2],
    "reform_simulation_run_id": SIDE_IDS[3],
}
# An economy report's answer, which the same 201 may carry.
CREATED_ECONOMY_REPORT = {
    "report_id": REPORT_ID,
    "report_run_id": RUN_ID,
    "status": "pending",
    "baseline_simulation_id": SIDE_IDS[0],
    "baseline_simulation_run_id": SIDE_IDS[1],
    "baseline_reused": False,
    "reform_simulation_id": SIDE_IDS[2],
    "reform_simulation_run_id": SIDE_IDS[3],
    "reform_reused": False,
}


def _create(status_code: int, body: dict, request: dict = HOUSEHOLD_REPORT_BODY):
    sent: list[dict] = []

    def answer(http_request: httpx.Request) -> httpx.Response:
        sent.append(httpx.Response(200, content=http_request.content).json())
        return httpx.Response(status_code, json=body)

    client = AuthenticatedClient(base_url="https://simulation.example", token="test")
    client.set_httpx_client(
        httpx.Client(
            base_url="https://simulation.example",
            transport=httpx.MockTransport(answer),
        )
    )
    response = create_report.sync_detailed(
        client=client, body=HouseholdReportRequest.from_dict(request)
    )
    return response, sent


def test_a_household_report_request_is_sent_as_given():
    response, sent = _create(201, CREATED_HOUSEHOLD_REPORT)

    assert sent == [HOUSEHOLD_REPORT_BODY]
    assert response.status_code == 201


def test_a_report_of_the_baseline_alone_is_sent_without_a_reform():
    request = {**HOUSEHOLD_REPORT_BODY, "reform": None}

    _, sent = _create(201, CREATED_HOUSEHOLD_REPORT, request)

    assert sent[0]["reform"] is None


def test_a_created_household_report_is_read_with_its_year_and_sides():
    response, _ = _create(201, CREATED_HOUSEHOLD_REPORT)

    assert isinstance(response.parsed, HouseholdReportCreated)
    assert response.parsed.status is ReportRunStatus.PENDING
    assert response.parsed.year == 2026
    assert response.parsed.to_dict() == CREATED_HOUSEHOLD_REPORT


def test_a_created_report_of_the_baseline_alone_has_no_reform():
    created = {
        **CREATED_HOUSEHOLD_REPORT,
        "reform_simulation_id": None,
        "reform_simulation_run_id": None,
    }

    response, _ = _create(201, created)

    assert isinstance(response.parsed, HouseholdReportCreated)
    assert response.parsed.reform_simulation_id is None
    assert response.parsed.reform_simulation_run_id is None


def test_a_household_report_rerun_is_read_with_its_new_run():
    client = AuthenticatedClient(base_url="https://simulation.example", token="test")
    client.set_httpx_client(
        httpx.Client(
            base_url="https://simulation.example",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(201, json=CREATED_HOUSEHOLD_REPORT)
            ),
        )
    )

    response = rerun_report.sync_detailed(
        UUID(REPORT_ID), client=client, body=ReportRerunRequest()
    )

    assert isinstance(response.parsed, HouseholdReportCreated)
    assert response.parsed.to_dict() == CREATED_HOUSEHOLD_REPORT


def test_a_created_economy_report_is_still_read_as_one():
    response, _ = _create(201, CREATED_ECONOMY_REPORT)

    assert isinstance(response.parsed, EconomyReportCreated)


def test_the_household_refusals_are_read_with_their_codes():
    refused, _ = _create(
        400,
        {
            "code": "unknown_household",
            "detail": "us has no stored household the request names.",
        },
    )
    unprocessable, _ = _create(
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
    )
    call_refused, _ = _create(
        503,
        {
            "code": "modal_call_refused",
            "detail": "Modal did not accept the report's call.",
            "report_id": REPORT_ID,
        },
    )

    assert isinstance(refused.parsed, EconomyError)
    assert refused.parsed.code == "unknown_household"
    assert isinstance(unprocessable.parsed, EconomyRequestUnprocessable)
    assert unprocessable.parsed.code == "household_not_calculable"
    assert isinstance(call_refused.parsed, EconomyError)
    assert str(call_refused.parsed.report_id) == REPORT_ID
