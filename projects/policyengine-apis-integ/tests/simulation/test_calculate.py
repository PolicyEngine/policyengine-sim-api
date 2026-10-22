"""Integration tests for Modal-based simulation calculations.

These tests run against the staging Modal deployment and verify
that economy-wide simulations complete successfully.
"""

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Protocol, TypeGuard, cast

import pytest
from policyengine_api_simulation_client import AuthenticatedClient, Client
from policyengine_api_simulation_client.models import (
    JobStage,
    JobStatusResponse,
    JobSubmitResponse,
    SimulationRequest,
)

from . import endpoint_calls
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
# From the generated client, so a stage added to the schema is accepted here.
_JOB_STAGES = frozenset(stage.value for stage in JobStage)


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
    bodies: list[Mapping] | None = None,
) -> JobStatusResponse:
    """Poll for job completion.

    Args:
        client: The API client
        job_id: The job ID to poll
        max_wait_seconds: Maximum time to wait for completion
        poll_interval: Time between polls in seconds
        bodies: When given, receives every running and complete status body

    Returns:
        The final JobStatusResponse

    Raises:
        TimeoutError: If job doesn't complete within max_wait_seconds
        AssertionError: If job fails

    """
    deadline = time.monotonic() + max_wait_seconds

    while time.monotonic() < deadline:
        response = endpoint_calls.job_status(client, job_id)

        if bodies is not None and response.status_code in {
            HTTPStatus.OK,
            HTTPStatus.ACCEPTED,
        }:
            bodies.append(json.loads(response.content))

        if response.status_code == HTTPStatus.OK:
            assert isinstance(response.parsed, JobStatusResponse)
            assert response.parsed.status == "complete", (
                f"Unexpected status: {response.parsed}"
            )
            return response.parsed

        if response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR:
            msg = f"Job failed: {response.content}"
            raise AssertionError(msg)

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
        msg = f"Unexpected status code: {response.status_code}"
        raise AssertionError(msg)

    msg = f"Job {job_id} did not complete within {max_wait_seconds}s"
    raise TimeoutError(msg)


def assert_job_progress_reported(bodies: list[Mapping]) -> None:
    """Verify the stage fields of ``GET /jobs/{job_id}`` (policyengine-sim-api#781).

    A gateway with stage reporting adds ``submitted_at`` to every new job. A
    Stage 12-only deployment runs these tests against the existing gateway,
    which may predate stage reporting; then there is nothing to verify.
    """
    assert bodies, "No status body was recorded"
    final = bodies[-1]
    if "submitted_at" not in final:
        return

    progress_seen: list[int] = []
    for body in bodies[:-1]:
        assert body["status"] == "running", f"Unexpected status body: {body}"
        assert body.get("stage") in _JOB_STAGES, f"Missing or unknown stage: {body}"
        progress = body["progress"]
        assert 0 <= progress <= body["progress_max"] <= 99, (
            f"Running progress out of range: {body}"
        )
        progress_seen.append(progress)
    assert progress_seen == sorted(progress_seen), (
        f"Progress moved backwards: {progress_seen}"
    )

    assert final["status"] == "complete"
    assert final.get("progress") == 100, f"Complete job without progress 100: {final}"
    assert "stage" not in final
    assert "progress_max" not in final
    for key in ("started_at", "finished_at"):
        assert key in final, f"Complete job without {key}: {final}"
    assert final.get("stage_durations"), (
        f"Complete job without stage durations: {final}"
    )


def submit_simulation_request(
    client: Client | AuthenticatedClient,
    request: SimulationRequest,
) -> JobSubmitResponse:
    response = endpoint_calls.submit_simulation(client, request)
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
        client: Client | AuthenticatedClient, job_id: str
    ) -> _StubJobStatusHttpResponse:
        del job_id, client
        return next(response_iterator)

    def record_sleep(seconds: float) -> None:
        sleep_seconds.append(seconds)

    monkeypatch.setattr(endpoint_calls, "job_status", get_status)
    monkeypatch.setattr(time, "sleep", record_sleep)
    result = poll_for_completion(
        cast("Client", object()),
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


def _running_body(stage: str, progress: int, progress_max: int) -> dict:
    return {
        "status": "running",
        "result": None,
        "error": None,
        "submitted_at": "2026-10-15T12:00:00Z",
        "stage": stage,
        "progress": progress,
        "progress_max": progress_max,
    }


_COMPLETE_BODY = {
    "status": "complete",
    "result": {},
    "submitted_at": "2026-10-15T12:00:00Z",
    "started_at": "2026-10-15T12:00:10Z",
    "finished_at": "2026-10-15T12:02:00Z",
    "progress": 100,
    "stage_durations": {"loading_data": 12.5, "baseline": 20.0, "reform": 60.0},
}


def test_poll_for_completion_records_running_and_complete_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = _running_body("reform", 67, 96)
    bodies: list[Mapping] = []
    response_iterator = iter(
        [
            _StubJobStatusHttpResponse(
                status_code=HTTPStatus.ACCEPTED,
                content=json.dumps(running).encode(),
            ),
            _StubJobStatusHttpResponse(
                status_code=HTTPStatus.OK,
                parsed=JobStatusResponse.from_dict(
                    {"status": "complete", "result": {}}
                ),
                content=json.dumps(_COMPLETE_BODY).encode(),
            ),
        ]
    )
    monkeypatch.setattr(
        endpoint_calls, "job_status", lambda client, job_id: next(response_iterator)
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    poll_for_completion(
        cast("Client", object()),
        "fc-test",
        max_wait_seconds=1.0,
        poll_interval=0.25,
        bodies=bodies,
    )

    assert bodies == [running, _COMPLETE_BODY]


def test_job_progress_check_accepts_a_reporting_job() -> None:
    assert_job_progress_reported(
        [
            _running_body("queued", 0, 24),
            _running_body("loading_data", 25, 54),
            _running_body("reform", 67, 96),
            _COMPLETE_BODY,
        ]
    )


def test_job_progress_check_rejects_progress_that_moves_backwards() -> None:
    with pytest.raises(AssertionError, match="backwards"):
        assert_job_progress_reported(
            [
                _running_body("reform", 67, 96),
                _running_body("loading_data", 25, 54),
                _COMPLETE_BODY,
            ]
        )


def test_job_progress_check_rejects_a_complete_job_without_progress() -> None:
    complete = {
        key: value for key, value in _COMPLETE_BODY.items() if key != "progress"
    }

    with pytest.raises(AssertionError, match="progress 100"):
        assert_job_progress_reported([complete])


def test_job_progress_check_rejects_a_running_job_without_a_stage() -> None:
    running = _running_body("reform", 67, 96)
    del running["stage"]

    with pytest.raises(AssertionError, match="stage"):
        assert_job_progress_reported([running, _COMPLETE_BODY])


def test_job_progress_check_skips_a_gateway_without_stage_reporting() -> None:
    assert_job_progress_reported(
        [
            {"status": "running", "result": None, "error": None},
            {"status": "complete", "result": {}},
        ]
    )


@pytest.mark.beta_only
def test_calculate_default_model(
    client: Client | AuthenticatedClient,
    max_wait_seconds: float,
    poll_interval: float,
):
    """A simulation on the default model version returns economic impact data.

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
    bodies: list[Mapping] = []
    result = poll_for_completion(
        client, job_id, max_wait_seconds, poll_interval, bodies=bodies
    )

    # Then - the job reported its stages while running
    assert_job_progress_reported(bodies)

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
    """A US state-region simulation finds its dataset in the bundled manifest.

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
    bodies: list[Mapping] = []
    result = poll_for_completion(
        client, job_id, max_wait_seconds, poll_interval, bodies=bodies
    )

    # Then - the job reported its stages while running
    assert_job_progress_reported(bodies)

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
    """A simulation pinned to a specific US model version returns impact data.

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
    bodies: list[Mapping] = []
    result = poll_for_completion(
        client, job_id, max_wait_seconds, poll_interval, bodies=bodies
    )

    # Then - the job reported its stages while running
    assert_job_progress_reported(bodies)

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
    """A UK simulation returns economic impact data.

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
    bodies: list[Mapping] = []
    result = poll_for_completion(
        client, job_id, max_wait_seconds, poll_interval, bodies=bodies
    )

    # Then - the job reported its stages while running
    assert_job_progress_reported(bodies)

    # Then - verify result structure
    assert result.status == "complete"
    assert result.result is not None

    economy_result = result.result
    assert_economy_result_sections(economy_result)
