"""
Unit tests for gateway endpoints.

Tests verify the endpoint correctly resolves app names and routes
simulation requests.
"""

import pytest
from fastapi.testclient import TestClient

pytest_plugins = ("tests.fixtures.endpoints",)


class TestGetAppName:
    """Tests for the get_app_name helper function."""

    def test__given_us_country_no_version__then_returns_latest_app(self, mock_modal):
        """
        Given country='us' and no version specified
        When get_app_name is called
        Then returns the app name for the 'latest' version.
        """
        from src.modal.gateway.endpoints import get_app_name

        # Given
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-us1-500-0-uk2-66-0",
        }

        # When
        app_name, resolved_version = get_app_name("us", None)

        # Then
        assert resolved_version == "1.500.0"
        assert app_name == "policyengine-simulation-us1-500-0-uk2-66-0"

    def test__given_us_country_with_version__then_returns_specified_app(
        self, mock_modal
    ):
        """
        Given country='us' and a specific version
        When get_app_name is called
        Then returns the app name for that version.
        """
        from src.modal.gateway.endpoints import get_app_name

        # Given
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "1.459.0": "policyengine-simulation-us1-459-0-uk2-65-9"
        }

        # When
        app_name, resolved_version = get_app_name("us", "1.459.0")

        # Then
        assert resolved_version == "1.459.0"
        assert app_name == "policyengine-simulation-us1-459-0-uk2-65-9"

    def test__given_uk_country__then_uses_uk_version_dict(self, mock_modal):
        """
        Given country='uk'
        When get_app_name is called
        Then uses the UK version dictionary.
        """
        from src.modal.gateway.endpoints import get_app_name

        # Given
        mock_modal["dicts"]["simulation-api-uk-versions"] = {
            "latest": "2.66.0",
            "2.66.0": "policyengine-simulation-us1-500-0-uk2-66-0",
        }

        # When
        app_name, resolved_version = get_app_name("uk", None)

        # Then
        assert resolved_version == "2.66.0"
        assert app_name == "policyengine-simulation-us1-500-0-uk2-66-0"

    def test__given_invalid_country__then_raises_value_error(self):
        """
        Given an invalid country code
        When get_app_name is called
        Then raises ValueError.
        """
        from src.modal.gateway.endpoints import get_app_name

        # When / Then
        with pytest.raises(ValueError, match="Unknown country"):
            get_app_name("invalid", None)

    def test__given_unknown_version__then_raises_value_error(self, mock_modal):
        """
        Given a version not in the registry
        When get_app_name is called
        Then raises ValueError.
        """
        from src.modal.gateway.endpoints import get_app_name

        # Given
        mock_modal["dicts"]["simulation-api-us-versions"] = {"1.459.0": "some-app"}

        # When / Then
        with pytest.raises(ValueError, match="Unknown version"):
            get_app_name("us", "9.9.9")


class TestSubmitSimulationEndpoint:
    """Tests for POST /simulate/economy/comparison endpoint."""

    def test__given_regular_data_value__then_routes_to_run_simulation(
        self, mock_modal, client: TestClient
    ):
        """
        Given a request with a data value
        When the simulation is submitted
        Then routes to run_simulation function.
        """
        # Given
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-us1-500-0-uk2-66-0",
        }

        request_body = {
            "country": "us",
            "scope": "macro",
            "reform": {},
            "data": "gs://policyengine-us-data/enhanced_cps_2024.h5",
        }

        # When
        response = client.post("/simulate/economy/comparison", json=request_body)

        # Then
        assert response.status_code == 200
        assert mock_modal["func"].last_from_name_call == (
            "policyengine-simulation-us1-500-0-uk2-66-0",
            "run_simulation",
        )

    def test__given_no_data_value__then_routes_to_run_simulation(
        self, mock_modal, client: TestClient
    ):
        """
        Given a request with no data field
        When the simulation is submitted
        Then routes to regular run_simulation function.
        """
        # Given
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-us1-500-0-uk2-66-0",
        }

        request_body = {
            "country": "us",
            "scope": "macro",
            "reform": {},
        }

        # When
        response = client.post("/simulate/economy/comparison", json=request_body)

        # Then
        assert response.status_code == 200
        assert mock_modal["func"].last_from_name_call == (
            "policyengine-simulation-us1-500-0-uk2-66-0",
            "run_simulation",
        )

    def test__given_submission__then_returns_job_id_and_poll_url(
        self, mock_modal, client: TestClient
    ):
        """
        Given a simulation submission
        When the request completes
        Then returns job_id and poll_url for async polling.
        """
        # Given
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-us1-500-0-uk2-66-0",
        }

        request_body = {
            "country": "us",
            "scope": "macro",
            "reform": {},
        }

        # When
        response = client.post("/simulate/economy/comparison", json=request_body)

        # Then
        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert data["job_id"] == "mock-job-id-123"
        assert data["poll_url"] == "/jobs/mock-job-id-123"
        assert data["status"] == "submitted"

    def test__given_submission_with_data__then_returns_resolved_bundle_metadata(
        self, mock_modal, client: TestClient
    ):
        """
        Given a simulation submission with an explicit data URI
        When the request completes
        Then the response exposes the resolved app and submitted dataset provenance.
        """
        # Given
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-us1-500-0-uk2-66-0",
        }

        request_body = {
            "country": "us",
            "scope": "macro",
            "reform": {},
            "data": "hf://policyengine/policyengine-us-data/enhanced_cps_2024.h5@1.77.0",
        }

        # When
        response = client.post("/simulate/economy/comparison", json=request_body)

        # Then
        assert response.status_code == 200
        data = response.json()
        assert data["resolved_app_name"] == "policyengine-simulation-us1-500-0-uk2-66-0"
        assert data["policyengine_bundle"] == {
            "model_version": "1.500.0",
            "policyengine_version": None,
            "data_version": None,
            "dataset": "hf://policyengine/policyengine-us-data/enhanced_cps_2024.h5@1.77.0",
        }

    def test__given_submitted_job__then_job_status_includes_bundle_metadata(
        self, mock_modal, client: TestClient
    ):
        """
        Given a submitted simulation job
        When polling job status
        Then the resolved bundle metadata is returned with the status response.
        """
        # Given
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-us1-500-0-uk2-66-0",
        }

        submit_response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "data": "hf://policyengine/policyengine-us-data/enhanced_cps_2024.h5@1.77.0",
            },
        )

        # When
        response = client.get(f"/jobs/{submit_response.json()['job_id']}")

        # Then
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "complete"
        assert data["resolved_app_name"] == "policyengine-simulation-us1-500-0-uk2-66-0"
        assert data["policyengine_bundle"] == {
            "model_version": "1.500.0",
            "policyengine_version": None,
            "data_version": None,
            "dataset": "hf://policyengine/policyengine-us-data/enhanced_cps_2024.h5@1.77.0",
        }
