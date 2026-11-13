"""Hermetic wire checks of the household routes' generated client models."""

from uuid import UUID

import httpx
import pytest
from policyengine_api_simulation_client import AuthenticatedClient
from policyengine_api_simulation_client.api.default import (
    get_household_calculation,
    submit_household_calculation,
)
from policyengine_api_simulation_client.models import (
    HouseholdCalculationAccepted,
    HouseholdCalculationError,
    HouseholdCalculationRequest,
    HouseholdCalculationState,
    HouseholdJobStatus,
)

HOUSEHOLD_ID = "00000000-0000-4000-8000-0000000000bb"
JOB_ID = "00000000-0000-4000-8000-0000000000aa"


def test_omitted_spm_settings_and_axes_stay_omitted():
    """The service uses the saved selection only when the request sends none."""
    omitted = HouseholdCalculationRequest.from_dict(
        {"country": "us", "household_id": HOUSEHOLD_ID}
    ).to_dict()
    given = HouseholdCalculationRequest.from_dict(
        {
            "country": "us",
            "household_id": HOUSEHOLD_ID,
            "spm": {"scenario": "zero_real"},
            "axes": [[{"name": "age", "count": 3, "min": 20, "max": 60}]],
        }
    ).to_dict()

    assert "spm" not in omitted
    assert "axes" not in omitted
    assert "policy_id" not in omitted
    assert given["spm"] == {"scenario": "zero_real"}
    assert given["axes"] == [[{"name": "age", "count": 3, "min": 20, "max": 60}]]


def test_a_submission_answer_and_an_error_are_read():
    accepted = HouseholdCalculationAccepted.from_dict(
        {"job_id": JOB_ID, "status": "pending"}
    )
    refused = HouseholdCalculationError.from_dict(
        {
            "code": "modal_call_refused",
            "detail": "Modal did not accept the call.",
            "household_job_id": JOB_ID,
        }
    )

    assert accepted.status is HouseholdJobStatus.PENDING
    assert refused.to_dict()["household_job_id"] == JOB_ID


def test_a_job_keeps_its_result_document_whole():
    document = {
        "status": "succeeded",
        "schema_version": 1,
        "calculation_id": JOB_ID,
        "output": {"household": {"people": {"you": {"age": {"2026": 40}}}}},
    }
    state = {
        "job_id": JOB_ID,
        "status": "succeeded",
        "country": "us",
        "household_id": HOUSEHOLD_ID,
        "policy_id": None,
        "year": 2026,
        "policyengine_version": "6.17.1",
        "country_package_version": "2.2.1",
        "modal_call_id": "fc-1",
        "result": document,
        "error_message": None,
        "created_at": "2026-11-10T12:00:00Z",
        "started_at": "2026-11-10T12:00:05Z",
        "completed_at": "2026-11-10T12:00:30Z",
    }

    parsed = HouseholdCalculationState.from_dict(state)

    assert parsed.status is HouseholdJobStatus.SUCCEEDED
    assert parsed.modal_call_id == "fc-1"
    assert parsed.to_dict()["result"] == document


# What the service answers to an unexpected failure (test_household_routes.py).
UNEXPECTED_FAILURE_ANSWER = {
    "code": "internal_error",
    "detail": "The service could not answer the request.",
}


def _client_answering(status_code: int, body: dict) -> AuthenticatedClient:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status_code, json=body)
    )
    client = AuthenticatedClient(base_url="https://simulation.example", token="test")
    client.set_httpx_client(
        httpx.Client(base_url="https://simulation.example", transport=transport)
    )
    return client


@pytest.mark.parametrize("route", ["submit", "read"])
def test_a_500_is_read_as_a_household_calculation_error(route):
    """Every 500 of the household routes is the JSON error the document declares."""
    client = _client_answering(500, UNEXPECTED_FAILURE_ANSWER)

    if route == "submit":
        response = submit_household_calculation.sync_detailed(
            client=client,
            body=HouseholdCalculationRequest.from_dict(
                {"country": "us", "household_id": HOUSEHOLD_ID}
            ),
        )
    else:
        response = get_household_calculation.sync_detailed(UUID(JOB_ID), client=client)

    assert isinstance(response.parsed, HouseholdCalculationError)
    assert response.parsed.to_dict() == UNEXPECTED_FAILURE_ANSWER
