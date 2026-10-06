"""Tests for gateway Pydantic models."""

import pytest
from pydantic import ValidationError

from src.modal.gateway.models import (
    PingRequest,
    PingResponse,
    SimulationRequest,
    JobSubmitResponse,
    JobStatusResponse,
)


class TestPingRequest:
    """Tests for PingRequest model."""

    def test_ping_request_accepts_integer_value(self):
        """
        Given an integer value
        When creating a PingRequest
        Then the model is created with the value.
        """
        # Given
        value = 42

        # When
        request = PingRequest(value=value)

        # Then
        assert request.value == 42

    def test_ping_request_accepts_negative_value(self):
        """
        Given a negative integer value
        When creating a PingRequest
        Then the model is created with the negative value.
        """
        # Given
        value = -10

        # When
        request = PingRequest(value=value)

        # Then
        assert request.value == -10

    def test_ping_request_rejects_non_integer(self):
        """
        Given a non-integer value
        When creating a PingRequest
        Then a ValidationError is raised.
        """
        # Given
        value = "not_an_integer"

        # When/Then
        with pytest.raises(ValidationError):
            PingRequest(value=value)

    def test_ping_request_rejects_missing_value(self):
        """
        Given no value
        When creating a PingRequest
        Then a ValidationError is raised.
        """
        # When/Then
        with pytest.raises(ValidationError):
            PingRequest()


class TestPingResponse:
    """Tests for PingResponse model."""

    def test_ping_response_accepts_integer_incremented(self):
        """
        Given an integer incremented value
        When creating a PingResponse
        Then the model is created with the value.
        """
        # Given
        incremented = 43

        # When
        response = PingResponse(incremented=incremented)

        # Then
        assert response.incremented == 43

    def test_ping_response_serializes_correctly(self):
        """
        Given a PingResponse
        When converting to dict
        Then the correct structure is returned.
        """
        # Given
        response = PingResponse(incremented=100)

        # When
        result = response.model_dump()

        # Then
        assert result == {"incremented": 100}


class TestSimulationRequest:
    """Tests for SimulationRequest model."""

    def test_simulation_request_requires_country(self):
        """
        Given no country
        When creating a SimulationRequest
        Then a ValidationError is raised.
        """
        # When/Then
        with pytest.raises(ValidationError):
            SimulationRequest()

    def test_simulation_request_accepts_country_only(self):
        """
        Given only a country
        When creating a SimulationRequest
        Then the model is created with version as None.
        """
        # Given
        country = "us"

        # When
        request = SimulationRequest(country=country)

        # Then
        assert request.country == "us"
        assert request.version is None

    def test_simulation_request_accepts_country_and_version(self):
        """
        Given a country and version
        When creating a SimulationRequest
        Then the model is created with both values.
        """
        # Given
        country = "uk"
        version = "1.0.0"

        # When
        request = SimulationRequest(country=country, version=version)

        # Then
        assert request.country == "uk"
        assert request.version == "1.0.0"

    def test_simulation_request_allows_extra_fields(self):
        """
        Given extra fields (reform, region, etc.)
        When creating a SimulationRequest
        Then the model accepts the extra fields.
        """
        # Given
        data = {
            "country": "us",
            "region": "enhanced_us",
            "reform": {"some.parameter": {"2024-01-01": True}},
        }

        # When
        request = SimulationRequest(**data)

        # Then
        assert request.country == "us"
        # Extra fields should be accessible via model_dump
        dumped = request.model_dump()
        assert dumped["region"] == "enhanced_us"
        assert dumped["reform"] == {"some.parameter": {"2024-01-01": True}}


class TestJobSubmitResponse:
    """Tests for JobSubmitResponse model."""

    def test_job_submit_response_creates_with_all_fields(self):
        """
        Given all required fields
        When creating a JobSubmitResponse
        Then the model is created correctly.
        """
        # Given
        data = {
            "job_id": "fc-abc123",
            "status": "submitted",
            "poll_url": "/jobs/fc-abc123",
            "country": "us",
            "version": "1.459.0",
            "resolved_app_name": "policyengine-simulation-us1-459-0-uk2-65-9",
            "policyengine_bundle": {
                "model_version": "1.459.0",
                "policyengine_version": None,
                "data_version": None,
                "dataset": "hf://policyengine/policyengine-us-data/enhanced_cps_2024.h5@1.77.0",
            },
        }

        # When
        response = JobSubmitResponse(**data)

        # Then
        assert response.job_id == "fc-abc123"
        assert response.status == "submitted"
        assert response.poll_url == "/jobs/fc-abc123"
        assert response.country == "us"
        assert response.version == "1.459.0"
        assert response.resolved_app_name == "policyengine-simulation-us1-459-0-uk2-65-9"
        assert response.policyengine_bundle.model_version == "1.459.0"
        assert response.policyengine_bundle.policyengine_version is None
        assert response.policyengine_bundle.dataset == (
            "hf://policyengine/policyengine-us-data/enhanced_cps_2024.h5@1.77.0"
        )


class TestJobStatusResponse:
    """Tests for JobStatusResponse model."""

    def test_job_status_response_complete_with_result(self):
        """
        Given a completed job with result
        When creating a JobStatusResponse
        Then the model contains the result.
        """
        # Given
        result = {"budget": {"total": 1000000}}

        # When
        response = JobStatusResponse(status="complete", result=result)

        # Then
        assert response.status == "complete"
        assert response.result == {"budget": {"total": 1000000}}
        assert response.error is None

    def test_job_status_response_running_without_result(self):
        """
        Given a running job
        When creating a JobStatusResponse
        Then result and error are None.
        """
        # When
        response = JobStatusResponse(status="running")

        # Then
        assert response.status == "running"
        assert response.result is None
        assert response.error is None

    def test_job_status_response_failed_with_error(self):
        """
        Given a failed job with error message
        When creating a JobStatusResponse
        Then the error is captured.
        """
        # Given
        error_msg = "Simulation timed out"

        # When
        response = JobStatusResponse(status="failed", error=error_msg)

        # Then
        assert response.status == "failed"
        assert response.result is None
        assert response.error == "Simulation timed out"

    def test_job_status_response_accepts_bundle_metadata(self):
        response = JobStatusResponse(
            status="complete",
            result={"budget": {"total": 1000000}},
            resolved_app_name="policyengine-simulation-us1-459-0-uk2-65-9",
            policyengine_bundle={
                "model_version": "1.459.0",
                "policyengine_version": None,
                "data_version": None,
                "dataset": "hf://policyengine/policyengine-us-data/enhanced_cps_2024.h5@1.77.0",
            },
        )

        assert response.resolved_app_name == "policyengine-simulation-us1-459-0-uk2-65-9"
        assert response.policyengine_bundle is not None
        assert response.policyengine_bundle.dataset == (
            "hf://policyengine/policyengine-us-data/enhanced_cps_2024.h5@1.77.0"
        )
