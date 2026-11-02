"""Tests for submitting a simulation: routing, spawning and observability ids."""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from fixtures.gateway_responses import OBSERVABILITY_ID, expected_bundle
from policyengine_simulation_observability.identifiers import OBSERVABILITY_ID_HEADER


class TestSubmitSimulationEndpoint:
    """Tests for POST /simulate/economy/comparison endpoint."""

    def test__given_data_value__then_rejects_before_spawning(
        self, mock_modal, client: TestClient
    ):
        """Given a data value, when submitting, then validation rejects it with 422."""
        # Given
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        request_body = {
            "country": "us",
            "scope": "macro",
            "reform": {},
            "data": "populace_us_2024",
        }

        # When
        response = client.post("/simulate/economy/comparison", json=request_body)

        # Then
        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "extra_forbidden"
        assert mock_modal["func"].last_payload is None

    def test__given_no_data_value__then_routes_to_run_simulation(
        self, mock_modal, client: TestClient
    ):
        """Given no data field, when submitting, then route to run_simulation."""
        # Given
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
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
            "policyengine-simulation-py4-10-0",
            "run_simulation",
        )
        assert "time_period" not in mock_modal["func"].last_payload
        assert "data_version" not in mock_modal["func"].last_payload

    def test__given_submission__then_returns_job_id_and_poll_url(
        self, mock_modal, client: TestClient
    ):
        """Given a submission, when it completes, then return job_id and poll_url."""
        # Given
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
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
        assert "observability_id" not in data
        generated_observability_id = response.headers[OBSERVABILITY_ID_HEADER]
        assert str(UUID(generated_observability_id)) == generated_observability_id
        assert (
            mock_modal["func"].last_observability_context["observability_id"]
            == generated_observability_id
        )
        assert "_observability_context" not in mock_modal["func"].last_payload

    def test__given_submission_with_include_cliffs__then_forwards_worker_flag(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "include_cliffs": True,
            },
        )

        assert response.status_code == 200
        assert mock_modal["func"].last_payload["include_cliffs"] is True

    @pytest.mark.parametrize(
        "field",
        ["observability_id", "run_id", "request_id", "traceparent"],
    )
    def test__given_identity_in_body_telemetry__then_uses_only_header(
        self, field: str, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }
        response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "_telemetry": {
                    field: "identifier",
                    "submission_claim_id": "claim-123",
                },
            },
            headers={OBSERVABILITY_ID_HEADER: OBSERVABILITY_ID},
        )

        assert response.status_code == 200
        assert response.headers[OBSERVABILITY_ID_HEADER] == OBSERVABILITY_ID
        spawned = mock_modal["func"].last_payload
        assert field not in spawned["_telemetry"]
        assert spawned["_telemetry"]["submission_claim_id"] == "claim-123"
        assert (
            mock_modal["func"].last_observability_context["observability_id"]
            == OBSERVABILITY_ID
        )
        assert "_observability_context" not in spawned

    def test__given_legacy_process_id__then_maps_submission_claim_id(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }
        response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "_telemetry": {"process_id": "legacy-claim-123"},
            },
            headers={OBSERVABILITY_ID_HEADER: OBSERVABILITY_ID},
        )

        assert response.status_code == 200
        assert (
            mock_modal["func"].last_payload["_telemetry"]["submission_claim_id"]
            == "legacy-claim-123"
        )

    def test__given_submission_header__then_propagates_observability_id(
        self, mock_modal, client: TestClient
    ):
        """Propagate the diagnostic identifier header to Modal and the response.

        Given a simulation submission with the diagnostic identifier header
        When the request completes
        Then the Modal context and response header preserve the identifier.
        """
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        request_body = {
            "country": "us",
            "scope": "macro",
            "reform": {},
            "_telemetry": {
                "submission_claim_id": "proc-123",
                "capture_mode": "disabled",
            },
        }

        response = client.post(
            "/simulate/economy/comparison",
            json=request_body,
            headers={OBSERVABILITY_ID_HEADER: OBSERVABILITY_ID},
        )

        assert response.status_code == 200
        data = response.json()
        assert "observability_id" not in data
        assert response.headers[OBSERVABILITY_ID_HEADER] == OBSERVABILITY_ID
        assert (
            mock_modal["func"].last_observability_context["observability_id"]
            == OBSERVABILITY_ID
        )
        assert "_observability_context" not in mock_modal["func"].last_payload
        assert "observability_id" not in mock_modal["func"].last_payload["_telemetry"]

    def test__given_submission_without_data__then_returns_default_bundle_metadata(
        self, mock_modal, client: TestClient
    ):
        """Report the resolved app and default dataset provenance on submission.

        Given a simulation submission without a dataset override
        When the request completes
        Then the response exposes the resolved app and submitted dataset provenance.
        """
        # Given
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
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
        assert data["resolved_app_name"] == "policyengine-simulation-py4-10-0"
        assert data["policyengine_bundle"] == expected_bundle("us", "1.500.0")
        assert "data" not in mock_modal["func"].last_payload
