"""Tests for polling a submitted simulation job's status."""

import pytest
from fastapi.testclient import TestClient
from fixtures.gateway_responses import (
    LOCAL_AREA_BREAKDOWN_RESULT,
    OBSERVABILITY_ID,
    expected_bundle,
)
from policyengine_simulation_observability.identifiers import OBSERVABILITY_ID_HEADER


class TestSubmitSimulationEndpoint:
    """Tests for POST /simulate/economy/comparison endpoint."""

    def test__given_submitted_job__then_job_status_includes_bundle_metadata(
        self, mock_modal, client: TestClient
    ):
        """Given a submitted job, when polling, then return its bundle metadata."""
        # Given
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        submit_response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
            },
        )

        # When
        response = client.get(f"/jobs/{submit_response.json()['job_id']}")

        # Then
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "complete"
        assert "observability_id" not in data
        assert (
            response.headers[OBSERVABILITY_ID_HEADER]
            == (submit_response.headers[OBSERVABILITY_ID_HEADER])
        )
        assert data["resolved_app_name"] == "policyengine-simulation-py4-10-0"
        assert data["policyengine_bundle"] == expected_bundle("us", "1.500.0")

    def test__given_submitted_job_with_header__then_polling_echoes_header(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        submit_response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "_telemetry": {
                    "submission_claim_id": "proc-123",
                    "capture_mode": "disabled",
                },
            },
            headers={OBSERVABILITY_ID_HEADER: OBSERVABILITY_ID},
        )

        response = client.get(
            f"/jobs/{submit_response.json()['job_id']}",
            headers={OBSERVABILITY_ID_HEADER: "00000000-0000-4000-8000-000000000099"},
        )

        assert response.status_code == 200
        assert "observability_id" not in response.json()
        assert response.headers[OBSERVABILITY_ID_HEADER] == OBSERVABILITY_ID

    @pytest.mark.usefixtures("mock_modal")
    def test__given_unknown_job_id__then_polling_returns_404_without_exception(
        self, client: TestClient, monkeypatch
    ):
        """Return 404 for a job ID the gateway never issued, without asking Modal.

        Given a job id that the gateway never issued
        When polling job status
        Then the gateway returns 404 before asking Modal for a call result,
        and records no exception, because the 404 is a client error.
        """
        recorded_errors = []
        monkeypatch.setattr(
            client.app.state.policyengine_observability,
            "record_exception",
            lambda exc, **kwargs: recorded_errors.append((exc, kwargs)),
        )

        response = client.get(
            "/jobs/unknown-job-id",
            headers={OBSERVABILITY_ID_HEADER: OBSERVABILITY_ID},
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Job not found: unknown-job-id"
        assert OBSERVABILITY_ID_HEADER not in response.headers
        assert recorded_errors == []

    def test__given_lazy_modal_call_without_metadata__then_polling_returns_404(
        self, mock_modal, client: TestClient
    ):
        """Return 404 for a job ID with no gateway metadata, even if Modal has one.

        Given Modal can construct a FunctionCall handle for an arbitrary id
        When the gateway has no metadata for that id
        Then the gateway still treats it as not found.
        """
        mock_modal["func"].call_for("auth-smoke-probe-does-not-exist")

        response = client.get("/jobs/auth-smoke-probe-does-not-exist")

        assert response.status_code == 404
        assert (
            response.json()["detail"]
            == "Job not found: auth-smoke-probe-does-not-exist"
        )

    def test__given_legacy_job_without_identifier__then_poll_does_not_create_one(
        self,
        mock_modal,
        client: TestClient,
    ):
        job_id = "legacy-job-without-observability-id"
        mock_modal["dicts"]["simulation-api-job-metadata"] = {
            job_id: {
                "resolved_app_name": "legacy-worker",
                "policyengine_bundle": {"model_version": "1.500.0"},
            }
        }
        mock_modal["func"].call_for(job_id)

        response = client.get(
            f"/jobs/{job_id}",
            headers={OBSERVABILITY_ID_HEADER: OBSERVABILITY_ID},
        )

        assert response.status_code == 200
        assert OBSERVABILITY_ID_HEADER not in response.headers

    def test__given_complete_local_area_breakdown__then_polling_returns_it(
        self, mock_modal, client: TestClient
    ):
        submit_response = client.post(
            "/simulate/economy/comparison",
            json={"country": "uk", "scope": "macro", "region": "uk", "reform": {}},
        )
        mock_modal["func"].last_call.result = LOCAL_AREA_BREAKDOWN_RESULT

        response = client.get(f"/jobs/{submit_response.json()['job_id']}")

        assert response.status_code == 200
        assert response.json()["status"] == "complete"
        assert response.json()["result"] == LOCAL_AREA_BREAKDOWN_RESULT

    def test__given_running_job__then_polling_returns_202(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        submit_response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
            },
        )
        job_id = submit_response.json()["job_id"]
        mock_modal["func"].last_call.running = True

        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 202
        assert response.json()["status"] == "running"

    def test__given_expired_modal_output__then_polling_returns_404(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        submit_response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
            },
        )
        job_id = submit_response.json()["job_id"]
        mock_modal["func"].last_call.error = mock_modal[
            "exception"
        ].OutputExpiredError()

        # Modal's FastAPI job queue example maps OutputExpiredError to 404:
        # https://modal.com/docs/guide/job-queue#integration-with-web-frameworks
        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 404
        assert response.json()["detail"] == f"Job not found: {job_id}"

    def test__given_modal_call_not_found__then_polling_returns_404(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        submit_response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
            },
        )
        job_id = submit_response.json()["job_id"]
        mock_modal["function_call"].from_id_errors[job_id] = mock_modal[
            "exception"
        ].NotFoundError()

        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 404
        assert response.json()["detail"] == f"Job not found: {job_id}"

    def test__given_worker_error__then_polling_returns_redacted_500(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        submit_response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
            },
        )
        job_id = submit_response.json()["job_id"]
        mock_modal["func"].last_call.error = RuntimeError("worker crashed")

        response = client.get(f"/jobs/{job_id}")

        assert response.status_code == 500
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"].startswith("Simulation failed")
        assert "correlation_id=" in body["error"]
        assert "worker crashed" not in body["error"]
