"""Hermetic wire checks of the household routes' generated client models."""

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
        "result": document,
        "error_message": None,
        "created_at": "2026-11-10T12:00:00Z",
        "started_at": "2026-11-10T12:00:05Z",
        "completed_at": "2026-11-10T12:00:30Z",
    }

    parsed = HouseholdCalculationState.from_dict(state)

    assert parsed.status is HouseholdJobStatus.SUCCEEDED
    assert parsed.to_dict()["result"] == document
