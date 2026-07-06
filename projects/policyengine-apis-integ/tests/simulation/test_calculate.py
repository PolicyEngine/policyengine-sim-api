"""
Integration tests for Modal-based simulation calculations.

These tests run against the staging Modal deployment and verify
that economy-wide simulations complete successfully.
"""

import time
from collections.abc import Mapping
from http import HTTPStatus

import pytest

from policyengine_api_simulation_client import AuthenticatedClient, Client
from policyengine_api_simulation_client.api.default import (
    get_job_status_jobs_job_id_get,
    submit_simulation_simulate_economy_comparison_post,
)
from policyengine_api_simulation_client.models import (
    JobStatusResponse,
    JobSubmitResponse,
    SimulationRequest,
)

_REQUIRED_ECONOMY_RESULT_SECTIONS = {"budget", "poverty", "inequality"}
_REQUIRED_DISTRICT_RESULT_KEYS = {
    "district",
    "average_household_income_change",
    "relative_household_income_change",
    "winner_percentage",
    "loser_percentage",
    "no_change_percentage",
    "population",
}
_AT_LARGE_DISTRICT_IDS = {"AK-01", "DC-01", "DE-01", "ND-01", "SD-01", "VT-01", "WY-01"}


def poll_for_completion(
    client: Client | AuthenticatedClient,
    job_id: str,
    max_wait_seconds: float,
    poll_interval: float,
) -> JobStatusResponse:
    """
    Poll for job completion.

    Args:
        client: The API client
        job_id: The job ID to poll
        max_wait_seconds: Maximum time to wait for completion
        poll_interval: Time between polls in seconds

    Returns:
        The final JobStatusResponse

    Raises:
        TimeoutError: If job doesn't complete within max_wait_seconds
        AssertionError: If job fails
    """
    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        response = get_job_status_jobs_job_id_get.sync_detailed(
            job_id=job_id, client=client
        )

        if response.status_code == HTTPStatus.OK:
            assert isinstance(response.parsed, JobStatusResponse)
            assert response.parsed.status == "complete", (
                f"Unexpected status: {response.parsed}"
            )
            return response.parsed

        if response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR:
            raise AssertionError(f"Job failed: {response.content}")

        if response.status_code == HTTPStatus.ACCEPTED:
            # Still running, wait and retry
            time.sleep(poll_interval)
            continue

        # Unexpected status code
        raise AssertionError(f"Unexpected status code: {response.status_code}")

    raise TimeoutError(f"Job {job_id} did not complete within {max_wait_seconds}s")


def submit_simulation_request(
    client: Client | AuthenticatedClient,
    request: SimulationRequest,
) -> JobSubmitResponse:
    response = submit_simulation_simulate_economy_comparison_post.sync_detailed(
        client=client,
        body=request,
    )
    assert response.status_code == HTTPStatus.OK, (
        f"Simulation submit failed with status {response.status_code}: "
        f"{response.content!r}"
    )
    assert isinstance(response.parsed, JobSubmitResponse), (
        f"Unexpected response type: {type(response.parsed)}"
    )
    return response.parsed


def assert_economy_result_sections(economy_result: object) -> None:
    assert isinstance(economy_result, Mapping), (
        f"Expected economy result to be an object, got {type(economy_result)}"
    )
    missing = _REQUIRED_ECONOMY_RESULT_SECTIONS - set(economy_result)
    assert not missing, (
        f"Missing expected economy result sections {sorted(missing)} "
        f"in result: {economy_result.keys()}"
    )


def assert_congressional_district_results(
    economy_result: object,
    *,
    expected_district_prefix: str | None = None,
    expected_district_ids: set[str] | None = None,
) -> None:
    assert isinstance(economy_result, Mapping), (
        f"Expected economy result to be an object, got {type(economy_result)}"
    )
    assert "congressional_district_impact" in economy_result, (
        f"Missing 'congressional_district_impact' in result: {economy_result.keys()}"
    )

    impact = economy_result["congressional_district_impact"]
    assert isinstance(impact, Mapping), (
        f"Expected congressional_district_impact to be an object, got {type(impact)}"
    )
    districts = impact.get("districts")
    assert isinstance(districts, list), (
        "Expected congressional_district_impact.districts to be a list, "
        f"got {type(districts)}"
    )
    assert districts, "Expected congressional_district_impact.districts to be non-empty"

    for district in districts:
        assert isinstance(district, Mapping), (
            "Expected each congressional district result to be an object, "
            f"got {type(district)}"
        )
        missing = _REQUIRED_DISTRICT_RESULT_KEYS - set(district)
        assert not missing, (
            f"Missing expected district result keys {sorted(missing)} "
            f"in district result: {district}"
        )
        assert isinstance(district["district"], str)

    if expected_district_prefix is not None:
        district_ids = [district["district"] for district in districts]
        assert all(
            district_id.startswith(expected_district_prefix)
            for district_id in district_ids
        ), (
            f"Expected all district IDs to start with {expected_district_prefix!r}, "
            f"got {district_ids}"
        )

    if expected_district_ids is not None:
        district_ids = {district["district"] for district in districts}
        missing = expected_district_ids - district_ids
        assert not missing, (
            f"Missing expected district IDs {sorted(missing)} "
            f"from result IDs: {sorted(district_ids)}"
        )


@pytest.mark.beta_only
def test_calculate_default_model(
    client: Client | AuthenticatedClient,
    max_wait_seconds: float,
    poll_interval: float,
):
    """
    Given a simulation request with default model version
    When the simulation is submitted and polled to completion
    Then the result contains expected economic impact data.
    """
    # Given
    request = SimulationRequest.from_dict(
        {
            "country": "us",
            "scope": "macro",
            "reform": {
                "gov.irs.credits.ctc.refundable.fully_refundable": {
                    "2023-01-01.2100-12-31": True
                }
            },
        }
    )

    # When - submit job
    submit_response = submit_simulation_request(client, request)
    job_id = submit_response.job_id

    # When - poll for completion
    result = poll_for_completion(client, job_id, max_wait_seconds, poll_interval)

    # Then - verify result structure
    assert result.status == "complete"
    assert result.result is not None

    economy_result = result.result
    assert_economy_result_sections(economy_result)
    assert_congressional_district_results(
        economy_result,
        expected_district_ids=_AT_LARGE_DISTRICT_IDS,
    )


@pytest.mark.beta_only
def test_calculate_us_state_region_model(
    client: Client | AuthenticatedClient,
    us_model_version: str,
    max_wait_seconds: float,
    poll_interval: float,
):
    """
    Given a US state-region simulation request
    When the simulation is submitted and polled to completion
    Then the worker can load the region dataset from its bundled manifest.
    """
    # Given
    request = SimulationRequest.from_dict(
        {
            "country": "us",
            "version": us_model_version,
            "region": "state/ut",
            "scope": "macro",
            "reform": {
                "gov.irs.credits.ctc.refundable.fully_refundable": {
                    "2023-01-01.2100-12-31": True
                }
            },
            "time_period": "2026",
        }
    )

    # When - submit job
    submit_response = submit_simulation_request(client, request)
    assert submit_response.version == us_model_version
    job_id = submit_response.job_id

    # When - poll for completion
    result = poll_for_completion(client, job_id, max_wait_seconds, poll_interval)

    # Then - verify result structure
    assert result.status == "complete"
    assert result.result is not None

    economy_result = result.result
    assert_economy_result_sections(economy_result)
    assert_congressional_district_results(
        economy_result,
        expected_district_prefix="UT-",
    )


@pytest.mark.beta_only
def test_calculate_specific_model(
    client: Client | AuthenticatedClient,
    us_model_version: str,
    max_wait_seconds: float,
    poll_interval: float,
):
    """
    Given a simulation request with a specific model version
    When the simulation is submitted and polled to completion
    Then the result contains expected economic impact data.
    """
    # Given
    request = SimulationRequest.from_dict(
        {
            "country": "us",
            "version": us_model_version,
            "region": "state/ut",
            "scope": "macro",
            "reform": {
                "gov.irs.credits.ctc.refundable.fully_refundable": {
                    "2023-01-01.2100-12-31": True
                }
            },
            "time_period": "2026",
        }
    )

    # When - submit job
    submit_response = submit_simulation_request(client, request)
    assert submit_response.version == us_model_version, (
        f"Version mismatch: expected {us_model_version}, got {submit_response.version}"
    )
    job_id = submit_response.job_id

    # When - poll for completion
    result = poll_for_completion(client, job_id, max_wait_seconds, poll_interval)

    # Then - verify result structure
    assert result.status == "complete"
    assert result.result is not None

    economy_result = result.result
    assert_economy_result_sections(economy_result)
    assert_congressional_district_results(
        economy_result,
        expected_district_prefix="UT-",
    )


@pytest.mark.beta_only
def test_calculate_uk_model(
    client: Client | AuthenticatedClient,
    uk_model_version: str,
    max_wait_seconds: float,
    poll_interval: float,
):
    """
    Given a UK simulation request
    When the simulation is submitted and polled to completion
    Then the result contains expected economic impact data.
    """
    # Given
    request = SimulationRequest.from_dict(
        {
            "country": "uk",
            "version": uk_model_version,
            "scope": "macro",
            "reform": {
                "gov.hmrc.income_tax.rates.uk[0].rate": {"2023-01-01.2100-12-31": 0.21}
            },
        }
    )

    # When - submit job
    submit_response = submit_simulation_request(client, request)
    assert submit_response.version == uk_model_version, (
        f"Version mismatch: expected {uk_model_version}, got {submit_response.version}"
    )
    job_id = submit_response.job_id

    # When - poll for completion
    result = poll_for_completion(client, job_id, max_wait_seconds, poll_interval)

    # Then - verify result structure
    assert result.status == "complete"
    assert result.result is not None

    economy_result = result.result
    assert_economy_result_sections(economy_result)
