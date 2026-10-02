"""
Integration tests for Modal-based simulation calculations.

These tests run against the staging Modal deployment and verify
that economy-wide simulations complete successfully.
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Protocol, TypeGuard, cast

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

from .polling import poll_retry_delay_seconds

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


class _ObjectWithToDict(Protocol):
    def to_dict(self) -> object: ...


def _has_to_dict(value: object) -> TypeGuard[_ObjectWithToDict]:
    return callable(getattr(value, "to_dict", None))


def _as_mapping(value: object, *, label: str) -> Mapping:
    if _has_to_dict(value):
        value = value.to_dict()

    assert isinstance(value, Mapping), (
        f"Expected {label} to be an object, got {type(value)}"
    )
    return value


def _district_ids(district_results: list[Mapping]) -> list[str]:
    district_ids: list[str] = []
    for district in district_results:
        district_id = district["district"]
        assert isinstance(district_id, str)
        district_ids.append(district_id)
    return district_ids


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
    deadline = time.monotonic() + max_wait_seconds

    while time.monotonic() < deadline:
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

        delay_seconds = poll_retry_delay_seconds(
            response.status_code,
            response.headers,
            fallback_seconds=poll_interval,
        )
        if delay_seconds is not None:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                break
            time.sleep(min(delay_seconds, remaining_seconds))
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
    economy_result = _as_mapping(economy_result, label="economy result")
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
    economy_result = _as_mapping(economy_result, label="economy result")
    assert "congressional_district_impact" in economy_result, (
        f"Missing 'congressional_district_impact' in result: {economy_result.keys()}"
    )

    impact = _as_mapping(
        economy_result["congressional_district_impact"],
        label="congressional_district_impact",
    )
    districts = impact.get("districts")
    assert isinstance(districts, list), (
        "Expected congressional_district_impact.districts to be a list, "
        f"got {type(districts)}"
    )
    assert districts, "Expected congressional_district_impact.districts to be non-empty"

    district_results = [
        _as_mapping(district, label="congressional district result")
        for district in districts
    ]

    for district in district_results:
        missing = _REQUIRED_DISTRICT_RESULT_KEYS - set(district)
        assert not missing, (
            f"Missing expected district result keys {sorted(missing)} "
            f"in district result: {district}"
        )
        assert isinstance(district["district"], str)

    if expected_district_prefix is not None:
        district_ids = _district_ids(district_results)
        assert all(
            district_id.startswith(expected_district_prefix)
            for district_id in district_ids
        ), (
            f"Expected all district IDs to start with {expected_district_prefix!r}, "
            f"got {district_ids}"
        )

    if expected_district_ids is not None:
        district_ids = set(_district_ids(district_results))
        missing = expected_district_ids - district_ids
        assert not missing, (
            f"Missing expected district IDs {sorted(missing)} "
            f"from result IDs: {sorted(district_ids)}"
        )


class _GeneratedClientObject:
    def __init__(self, payload: Mapping) -> None:
        self._payload = dict(payload)

    def to_dict(self) -> dict:
        return dict(self._payload)


def test_result_assertions_accept_generated_client_objects() -> None:
    result = _GeneratedClientObject(
        {
            "budget": {},
            "poverty": {},
            "inequality": {},
            "congressional_district_impact": _GeneratedClientObject(
                {
                    "districts": [
                        _GeneratedClientObject(
                            {
                                "district": "UT-01",
                                "average_household_income_change": 0.0,
                                "relative_household_income_change": 0.0,
                                "winner_percentage": 0.0,
                                "loser_percentage": 0.0,
                                "no_change_percentage": 1.0,
                                "population": 1.0,
                            }
                        )
                    ]
                }
            ),
        }
    )

    assert_economy_result_sections(result)
    assert_congressional_district_results(result, expected_district_prefix="UT-")


@dataclass
class _StubJobStatusHttpResponse:
    status_code: HTTPStatus
    parsed: JobStatusResponse | None = None
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


def _poll_with_responses(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_StubJobStatusHttpResponse],
    *,
    max_wait_seconds: float,
    poll_interval: float,
) -> tuple[JobStatusResponse, list[float]]:
    response_iterator = iter(responses)
    sleep_seconds: list[float] = []

    def get_status(
        *, job_id: str, client: Client | AuthenticatedClient
    ) -> _StubJobStatusHttpResponse:
        del job_id, client
        return next(response_iterator)

    def record_sleep(seconds: float) -> None:
        sleep_seconds.append(seconds)

    monkeypatch.setattr(
        get_job_status_jobs_job_id_get,
        "sync_detailed",
        get_status,
    )
    monkeypatch.setattr(time, "sleep", record_sleep)
    result = poll_for_completion(
        cast(Client, object()),
        "fc-test",
        max_wait_seconds=max_wait_seconds,
        poll_interval=poll_interval,
    )
    return result, sleep_seconds


@pytest.mark.parametrize(
    "status_code",
    [
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    ],
)
def test_poll_for_completion_retries_transient_gateway_statuses(
    monkeypatch: pytest.MonkeyPatch,
    status_code: HTTPStatus,
) -> None:
    complete = JobStatusResponse.from_dict({"status": "complete", "result": {}})
    result, sleep_seconds = _poll_with_responses(
        monkeypatch,
        [
            _StubJobStatusHttpResponse(status_code=status_code),
            _StubJobStatusHttpResponse(
                status_code=HTTPStatus.OK,
                parsed=complete,
            ),
        ],
        max_wait_seconds=1.0,
        poll_interval=0.25,
    )

    assert result is complete
    assert sleep_seconds == [0.25]


def test_poll_for_completion_honors_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete = JobStatusResponse.from_dict({"status": "complete", "result": {}})
    result, sleep_seconds = _poll_with_responses(
        monkeypatch,
        [
            _StubJobStatusHttpResponse(
                status_code=HTTPStatus.GATEWAY_TIMEOUT,
                headers={"retry-after": "10"},
            ),
            _StubJobStatusHttpResponse(
                status_code=HTTPStatus.OK,
                parsed=complete,
            ),
        ],
        max_wait_seconds=30.0,
        poll_interval=0.25,
    )

    assert result is complete
    assert sleep_seconds == [10.0]


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
