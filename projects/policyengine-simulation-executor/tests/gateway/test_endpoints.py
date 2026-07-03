"""
Unit tests for gateway endpoints.

Tests verify the endpoint correctly resolves app names and routes
simulation requests.
"""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from fixtures.gateway.test_endpoints import (
    TEST_APP_RELEASE_BUNDLE,
    TEST_ROUTING_STATE,
    resolve_test_dataset_uri,
)
from policyengine_simulation_executor.hf_dataset import HuggingFaceDatasetReferenceError


def expected_bundle(
    country: str,
    model_version: str,
    *,
    dataset: str | None = None,
    data_version: str | None = None,
) -> dict[str, str | None]:
    resolved_dataset = resolve_test_dataset_uri(country, dataset, data_version)
    if (
        data_version is not None
        and resolved_dataset is not None
        and resolved_dataset.startswith("hf://")
    ):
        resolved_dataset = (
            f"{resolved_dataset.rsplit('@', maxsplit=1)[0]}@{data_version}"
        )
    country_bundle = TEST_APP_RELEASE_BUNDLE[country]
    bundle: dict[str, str | None] = {
        "model_version": model_version,
        "policyengine_version": TEST_APP_RELEASE_BUNDLE["policyengine_version"],
        "data_version": data_version or country_bundle.get("data_version"),
        "dataset": resolved_dataset,
    }
    return {key: value for key, value in bundle.items() if value is not None}


class TestGetAppName:
    """Tests for the get_app_name helper function."""

    def test__given_no_version__then_prefers_latest_policyengine_bundle(
        self, mock_modal
    ):
        from src.modal.gateway.endpoints import get_app_name

        app_name, resolved_version = get_app_name("us", None)

        assert resolved_version == "4.10.0"
        assert app_name == "policyengine-simulation-py4-10-0"

    def test__given_legacy_version_matching_policyengine_bundle__then_routes_by_bundle(
        self, mock_modal
    ):
        from src.modal.gateway.endpoints import get_app_name

        app_name, resolved_version = get_app_name("uk", "4.10.0")

        assert resolved_version == "4.10.0"
        assert app_name == "policyengine-simulation-py4-10-0"

    def test__given_us_country_no_version__then_returns_latest_app(self, mock_modal):
        """
        Given country='us' and no version specified
        When get_app_name is called
        Then returns the app name for the 'latest' version.
        """
        from src.modal.gateway.endpoints import get_app_name

        # When
        app_name, resolved_version = get_app_name("us", None)

        # Then
        assert resolved_version == "4.10.0"
        assert app_name == "policyengine-simulation-py4-10-0"

    def test__given_us_country_with_version__then_returns_specified_app(
        self, mock_modal
    ):
        """
        Given country='us' and a specific version
        When get_app_name is called
        Then returns the app name for that version.
        """
        from src.modal.gateway.endpoints import get_app_name

        # When
        app_name, resolved_version = get_app_name("us", "1.459.0")

        # Then
        assert resolved_version == "1.459.0"
        assert app_name == "policyengine-simulation-py3-9-0"

    def test__given_uk_country__then_uses_uk_version_dict(self, mock_modal):
        """
        Given country='uk'
        When get_app_name is called
        Then uses the UK version dictionary.
        """
        from src.modal.gateway.endpoints import get_app_name

        # When
        app_name, resolved_version = get_app_name("uk", None)

        # Then
        assert resolved_version == "4.10.0"
        assert app_name == "policyengine-simulation-py4-10-0"

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

        # When / Then
        with pytest.raises(ValueError, match="Unknown version"):
            get_app_name("us", "9.9.9")

    def test__given_active_state_absent__then_falls_back_to_legacy_country_dict(
        self, mock_modal
    ):
        from src.modal.gateway.endpoints import get_app_name

        del mock_modal["dicts"]["simulation-api-routing-state"]
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "legacy-app",
        }

        app_name, resolved_version = get_app_name("us", None)

        assert resolved_version == "1.500.0"
        assert app_name == "legacy-app"

    def test__given_active_state_with_only_legacy_country_routes__then_no_version_uses_country_latest(
        self, mock_modal
    ):
        from src.modal.gateway.endpoints import get_app_name

        mock_modal["dicts"]["simulation-api-routing-state"] = {
            "active": {
                "schema_version": 1,
                "generation": "legacy-seed",
                "latest": {
                    "us": "1.715.2",
                    "uk": "2.88.20",
                },
                "routes": {
                    "policyengine": {},
                    "us": {"1.715.2": "policyengine-simulation-us1-715-2-uk2-88-20"},
                    "uk": {"2.88.20": "policyengine-simulation-us1-715-2-uk2-88-20"},
                },
                "bundles": {},
            }
        }

        app_name, resolved_version = get_app_name("us", None)

        assert resolved_version == "1.715.2"
        assert app_name == "policyengine-simulation-us1-715-2-uk2-88-20"

    def test__given_policyengine_version_field__then_routes_by_bundle(self, mock_modal):
        from src.modal.gateway.endpoints import resolve_route

        route = resolve_route("us", None, "4.10.0")

        assert route.response_version == "4.10.0"
        assert route.policyengine_version == "4.10.0"
        assert route.app_name == "policyengine-simulation-py4-10-0"


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
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        request_body = {
            "country": "us",
            "scope": "macro",
            "reform": {},
            "data": "gs://external-bucket/custom/file.h5",
        }

        # When
        response = client.post("/simulate/economy/comparison", json=request_body)

        # Then
        assert response.status_code == 200
        assert mock_modal["func"].last_from_name_call == (
            "policyengine-simulation-py4-10-0",
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
        """
        Given a simulation submission
        When the request completes
        Then returns job_id and poll_url for async polling.
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
        assert "job_id" in data
        assert data["job_id"] == "mock-job-id-123"
        assert data["poll_url"] == "/jobs/mock-job-id-123"
        assert data["status"] == "submitted"

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

    def test__given_submission_with_telemetry__then_preserves_run_id(
        self, mock_modal, client: TestClient
    ):
        """
        Given a simulation submission with internal telemetry metadata
        When the request completes
        Then the spawned payload preserves telemetry and the response echoes run_id.
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
                "run_id": "run-123",
                "process_id": "proc-123",
                "capture_mode": "disabled",
            },
        }

        response = client.post("/simulate/economy/comparison", json=request_body)

        assert response.status_code == 200
        data = response.json()
        assert data["run_id"] == "run-123"
        assert mock_modal["func"].last_payload["_telemetry"]["run_id"] == "run-123"

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
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        request_body = {
            "country": "us",
            "scope": "macro",
            "reform": {},
            "data": "gs://external-bucket/custom/file.h5@custom-v1",
        }

        # When
        response = client.post("/simulate/economy/comparison", json=request_body)

        # Then
        assert response.status_code == 200
        data = response.json()
        assert data["resolved_app_name"] == "policyengine-simulation-py4-10-0"
        assert data["policyengine_bundle"] == expected_bundle(
            "us",
            "1.500.0",
            dataset="gs://external-bucket/custom/file.h5@custom-v1",
            data_version="custom-v1",
        )

    def test__given_submission_with_dataset_name__then_bundle_dataset_uses_manifest_uri(
        self, mock_modal, client: TestClient
    ):
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

        response = client.post("/simulate/economy/comparison", json=request_body)

        assert response.status_code == 200
        data = response.json()
        assert data["policyengine_bundle"]["dataset"] == resolve_test_dataset_uri(
            "us", "populace_us_2024"
        )

    def test__given_legacy_alias_in_bundle_snapshot__then_gateway_still_resolves_it(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }
        state = deepcopy(TEST_ROUTING_STATE)
        state["bundles"]["4.10.0"]["us"]["dataset_aliases"] = {
            "legacy_populace_us": "populace_us_2024",
        }
        mock_modal["dicts"]["simulation-api-routing-state"] = {"active": state}

        response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "data": "legacy_populace_us",
            },
        )

        assert response.status_code == 200
        assert response.json()["policyengine_bundle"]["dataset"] == (
            resolve_test_dataset_uri("us", "populace_us_2024")
        )

    def test__given_us_state_region_without_data__then_keeps_contract_and_uses_default_dataset(
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
                "region": "state/UT",
                "reform": {},
            },
        )

        assert response.status_code == 200
        assert response.json()["policyengine_bundle"] == expected_bundle(
            "us",
            "1.500.0",
        )

    def test__given_submission_with_logical_revision__then_bundle_dataset_uses_revision(
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
                "data": "populace_us_2024@custom-v1",
            },
        )

        assert response.status_code == 200
        bundle = response.json()["policyengine_bundle"]
        assert bundle["dataset"] == (
            "hf://policyengine/populace-us/populace_us_2024.h5@custom-v1"
        )
        assert bundle["data_version"] == "custom-v1"

    def test__given_submission_with_explicit_uri_revision__then_bundle_data_version_uses_revision(
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
                "data": "gs://external-bucket/custom/file.h5@custom-v1",
            },
        )

        assert response.status_code == 200
        bundle = response.json()["policyengine_bundle"]
        assert bundle["dataset"] == "gs://external-bucket/custom/file.h5@custom-v1"
        assert bundle["data_version"] == "custom-v1"

    def test__given_submission_with_conflicting_data_versions__then_returns_400(
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
                "data": "populace_us_2024@custom-v1",
                "data_version": "custom-v2",
            },
        )

        assert response.status_code == 400
        assert mock_modal["func"].last_payload is None

    def test__given_submission_with_invalid_unmanaged_hf_revision__then_returns_400_before_spawn(
        self, mock_modal, client: TestClient, monkeypatch
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        def reject_revision(dataset_uri):
            raise HuggingFaceDatasetReferenceError("revision missing")

        monkeypatch.setattr(
            "policyengine_simulation_executor.dataset_uri.validate_hf_dataset_uri",
            reject_revision,
        )

        response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "data": "hf://external/example-data/file.h5@does-not-exist",
            },
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "revision missing"
        assert mock_modal["func"].last_payload is None

    def test__given_submission_with_uk_dataset_name__then_bundle_dataset_is_versioned_uri(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-uk-versions"] = {
            "latest": "2.66.0",
            "2.66.0": "policyengine-simulation-py4-10-0",
        }

        request_body = {
            "country": "uk",
            "scope": "macro",
            "reform": {},
            "data": "populace_uk_2023",
        }

        response = client.post("/simulate/economy/comparison", json=request_body)

        assert response.status_code == 200
        data = response.json()
        assert data["policyengine_bundle"]["dataset"] == resolve_test_dataset_uri(
            "uk", "populace_uk_2023"
        )

    def test__given_uk_submission_without_data_and_manifest_commit__then_uses_populace_default(
        self,
        mock_modal,
        client: TestClient,
        monkeypatch,
    ):
        app_name = "policyengine-simulation-py4-13-1"
        manifest_uri = (
            "hf://policyengine/populace-uk-private/"
            "populace_uk_2023.h5@uk-manifest-commit"
        )
        bundle = deepcopy(TEST_APP_RELEASE_BUNDLE)
        bundle["app_name"] = app_name
        bundle["policyengine_version"] = "4.13.1"
        bundle["uk"]["model_version"] = "2.88.20"
        bundle["uk"]["data_version"] = "populace-uk-2023-release"
        bundle["uk"]["data_artifact_revision"] = "uk-manifest-commit"
        bundle["uk"]["default_dataset_uri"] = manifest_uri
        bundle["uk"]["dataset_uris"]["populace_uk_2023"] = manifest_uri
        state = deepcopy(TEST_ROUTING_STATE)
        state["routes"]["policyengine"]["4.13.1"] = app_name
        state["routes"]["uk"]["2.88.20"] = app_name
        state["bundles"]["4.13.1"] = bundle
        mock_modal["dicts"]["simulation-api-routing-state"] = {"active": state}

        def reject_revision(dataset_uri, revision):
            raise HuggingFaceDatasetReferenceError(
                f"HF validation should not run for private GCS data: {dataset_uri}@{revision}"
            )

        monkeypatch.setattr(
            "policyengine_simulation_executor.dataset_uri.with_hf_revision",
            reject_revision,
        )

        response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "uk",
                "version": "2.88.20",
                "scope": "macro",
                "reform": {},
            },
        )

        assert response.status_code == 200
        assert response.json()["policyengine_bundle"]["dataset"] == manifest_uri

    def test__given_submission_with_runtime_bundle__then_accepts_internal_provenance(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        request_body = {
            "country": "us",
            "scope": "macro",
            "reform": {},
            "data": "populace_us_2024",
            "data_version": "custom-v2",
            "_runtime_bundle": {
                "model_version": "1.500.0",
                "data_version": "custom-v2",
            },
            "_metadata": {"process_id": "process-123"},
        }

        response = client.post("/simulate/economy/comparison", json=request_body)

        assert response.status_code == 200
        data = response.json()
        assert data["policyengine_bundle"] == expected_bundle(
            "us",
            "1.500.0",
            dataset="populace_us_2024",
            data_version="custom-v2",
        )
        assert mock_modal["func"].last_payload["data_version"] == "custom-v2"
        assert "_runtime_bundle" not in mock_modal["func"].last_payload
        assert "_metadata" not in mock_modal["func"].last_payload

    def test__given_submission_with_unknown_dataset_name__then_bundle_dataset_is_preserved(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        request_body = {
            "country": "us",
            "scope": "macro",
            "reform": {},
            "data": "custom_dataset_label",
        }

        response = client.post("/simulate/economy/comparison", json=request_body)

        assert response.status_code == 200
        data = response.json()
        assert data["policyengine_bundle"]["dataset"] == "custom_dataset_label"

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
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        submit_response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "data": "gs://external-bucket/custom/file.h5@custom-v1",
            },
        )

        # When
        response = client.get(f"/jobs/{submit_response.json()['job_id']}")

        # Then
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "complete"
        assert "run_id" not in data
        assert data["resolved_app_name"] == "policyengine-simulation-py4-10-0"
        assert data["policyengine_bundle"] == expected_bundle(
            "us",
            "1.500.0",
            dataset="gs://external-bucket/custom/file.h5@custom-v1",
            data_version="custom-v1",
        )

    def test__given_submitted_job_with_telemetry__then_polling_echoes_run_id(
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
                    "run_id": "run-123",
                    "process_id": "proc-123",
                    "capture_mode": "disabled",
                },
            },
        )

        response = client.get(f"/jobs/{submit_response.json()['job_id']}")

        assert response.status_code == 200
        assert response.json()["run_id"] == "run-123"

    def test__given_unknown_job_id__then_polling_returns_404(
        self, mock_modal, client: TestClient, monkeypatch
    ):
        """
        Given a job id that the gateway never issued
        When polling job status
        Then the gateway returns 404 before asking Modal for a call result.
        """
        from src.modal.gateway import endpoints as endpoints_module

        recorded_errors = []
        monkeypatch.setattr(
            endpoints_module,
            "record_error",
            lambda exc, **kwargs: recorded_errors.append((exc, kwargs)),
        )

        response = client.get("/jobs/unknown-job-id")

        assert response.status_code == 404
        assert response.json()["detail"] == "Job not found: unknown-job-id"
        assert str(recorded_errors[0][0]) == "Job not found: unknown-job-id"
        assert recorded_errors[0][1] == {
            "handled": True,
            "status_code": 404,
            "include_stack": False,
        }

    def test__given_lazy_modal_call_without_metadata__then_polling_returns_404(
        self, mock_modal, client: TestClient
    ):
        """
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


class TestVersionEndpoints:
    def test__given_active_state__then_lists_policyengine_and_country_routes(
        self, mock_modal, client: TestClient
    ):
        response = client.get("/versions")

        assert response.status_code == 200
        data = response.json()
        assert data["policyengine"]["latest"] == "4.10.0"
        assert data["policyengine"]["4.10.0"] == "policyengine-simulation-py4-10-0"
        assert data["us"]["latest"] == "1.500.0"
        assert data["uk"]["latest"] == "2.66.0"

    def test__given_active_state__then_policyengine_versions_endpoint_works(
        self, mock_modal, client: TestClient
    ):
        response = client.get("/versions/policyengine")

        assert response.status_code == 200
        assert response.json()["latest"] == "4.10.0"

    def test__given_unknown_version_kind__then_records_handled_404(
        self, mock_modal, client: TestClient, monkeypatch
    ):
        from src.modal.gateway import endpoints as endpoints_module

        recorded_errors = []
        monkeypatch.setattr(
            endpoints_module,
            "record_error",
            lambda exc, **kwargs: recorded_errors.append((exc, kwargs)),
        )

        response = client.get("/versions/fr")

        assert response.status_code == 404
        assert response.json()["detail"] == "Unknown version kind: fr"
        assert str(recorded_errors[0][0]) == "Unknown version kind: fr"
        assert recorded_errors[0][1] == {
            "handled": True,
            "status_code": 404,
            "include_stack": False,
        }

    def test__given_no_active_state__then_versions_fall_back_to_old_dicts(
        self, mock_modal, client: TestClient
    ):
        del mock_modal["dicts"]["simulation-api-routing-state"]
        mock_modal["dicts"]["simulation-api-policyengine-versions"] = {
            "latest": "4.9.0",
            "4.9.0": "old-py-app",
        }
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.499.0",
            "1.499.0": "old-us-app",
        }
        mock_modal["dicts"]["simulation-api-uk-versions"] = {
            "latest": "2.65.0",
            "2.65.0": "old-uk-app",
        }

        response = client.get("/versions")

        assert response.status_code == 200
        assert response.json() == {
            "policyengine": {
                "latest": "4.9.0",
                "4.9.0": "old-py-app",
            },
            "us": {
                "latest": "1.499.0",
                "1.499.0": "old-us-app",
            },
            "uk": {
                "latest": "2.65.0",
                "2.65.0": "old-uk-app",
            },
        }


class TestBudgetWindowBatchEndpoints:
    """Tests for budget-window batch gateway endpoints."""

    def test__given_budget_window_submission__then_returns_parent_batch_job_id(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
                "max_parallel": 2,
            },
        )

        assert response.status_code == 200
        assert mock_modal["func"].last_from_name_call == (
            "policyengine-simulation-py4-10-0",
            "run_budget_window_batch",
        )
        assert response.json() == {
            "batch_job_id": "mock-batch-job-id-123",
            "status": "submitted",
            "poll_url": "/budget-window-jobs/mock-batch-job-id-123",
            "country": "us",
            "version": "4.10.0",
            "resolved_app_name": "policyengine-simulation-py4-10-0",
            "policyengine_bundle": expected_bundle("us", "1.500.0"),
        }

    def test__given_unknown_budget_window_job__then_records_handled_404(
        self, mock_modal, client: TestClient, monkeypatch
    ):
        from src.modal.gateway import endpoints as endpoints_module

        recorded_errors = []
        monkeypatch.setattr(
            endpoints_module,
            "record_error",
            lambda exc, **kwargs: recorded_errors.append((exc, kwargs)),
        )

        response = client.get("/budget-window-jobs/missing-batch")

        assert response.status_code == 404
        assert response.json()["detail"] == (
            "Budget-window job not found: missing-batch"
        )
        assert str(recorded_errors[0][0]) == (
            "Budget-window job not found: missing-batch"
        )
        assert recorded_errors[0][1] == {
            "handled": True,
            "status_code": 404,
            "include_stack": False,
        }

    def test__given_parent_lookup_failure__then_degraded_poll_is_not_an_error(
        self, mock_modal, client: TestClient, monkeypatch
    ):
        """
        Given a submitted batch whose parent FunctionCall lookup fails
        When polling batch status
        Then the gateway serves the seed state (202) and records a degradation
        event — NOT a request error, which would emit http_request_failed and
        error metrics contradicting the successful response.
        """
        from fixtures.gateway.test_endpoints import MockFunctionCall
        from src.modal.gateway import endpoints as endpoints_module

        recorded_errors = []
        recorded_events = []
        monkeypatch.setattr(
            endpoints_module,
            "record_error",
            lambda exc, **kwargs: recorded_errors.append((exc, kwargs)),
        )
        monkeypatch.setattr(
            endpoints_module,
            "record_event",
            lambda event, **kwargs: recorded_events.append((event, kwargs)),
        )

        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }
        submit_response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
            },
        )
        batch_job_id = submit_response.json()["batch_job_id"]
        MockFunctionCall.from_id_errors[batch_job_id] = RuntimeError(
            "transient modal control-plane error"
        )

        response = client.get(f"/budget-window-jobs/{batch_job_id}")

        assert response.status_code == 202
        assert response.json()["status"] == "submitted"
        assert recorded_errors == []
        assert recorded_events == [
            (
                "budget_window_parent_lookup_degraded",
                {
                    "batch_job_id": batch_job_id,
                    "error_type": "RuntimeError",
                    "parent_call_not_found": False,
                },
            )
        ]

    def test__given_budget_window_include_cliffs__then_returns_422(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
                "include_cliffs": True,
            },
        )

        assert response.status_code == 422
        assert "cliff impacts are not supported" in response.text
        assert mock_modal["func"].last_payload is None

    def test__given_budget_window_submission__then_initial_poll_returns_seed_state(
        self, mock_modal, client: TestClient
    ):
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        submit_response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
                "max_parallel": 2,
                "_telemetry": {
                    "run_id": "batch-run-123",
                    "process_id": "proc-123",
                    "capture_mode": "disabled",
                },
            },
        )

        response = client.get(
            f"/budget-window-jobs/{submit_response.json()['batch_job_id']}"
        )

        assert response.status_code == 202
        assert response.json() == {
            "status": "submitted",
            "progress": 0,
            "completed_years": [],
            "running_years": [],
            "queued_years": ["2026", "2027", "2028"],
            "failed_years": [],
            "child_jobs": {},
            "result": None,
            "error": None,
            "resolved_app_name": "policyengine-simulation-py4-10-0",
            "policyengine_bundle": expected_bundle("us", "1.500.0"),
            "run_id": "batch-run-123",
        }

    def test__given_batch_state__then_poll_returns_completed_response(
        self, mock_modal, client: TestClient
    ):
        from src.modal.budget_window_state import put_batch_job_state
        from src.modal.gateway.models import (
            BudgetWindowAnnualImpact,
            BudgetWindowBatchState,
            BudgetWindowResult,
            BudgetWindowTotals,
            PolicyEngineBundle,
        )

        put_batch_job_state(
            BudgetWindowBatchState(
                batch_job_id="mock-batch-job-id-123",
                status="complete",
                country="us",
                region="us",
                version="1.500.0",
                target="general",
                resolved_app_name="policyengine-simulation-py4-10-0",
                policyengine_bundle=PolicyEngineBundle(model_version="1.500.0"),
                start_year="2026",
                window_size=2,
                max_parallel=2,
                request_payload={"country": "us", "region": "us"},
                years=["2026", "2027"],
                queued_years=[],
                running_years=[],
                completed_years=["2026", "2027"],
                failed_years=[],
                child_jobs={},
                partial_annual_impacts={},
                result=BudgetWindowResult(
                    startYear="2026",
                    endYear="2027",
                    windowSize=2,
                    annualImpacts=[
                        BudgetWindowAnnualImpact(
                            year="2026",
                            taxRevenueImpact=10,
                            federalTaxRevenueImpact=7,
                            stateTaxRevenueImpact=3,
                            benefitSpendingImpact=5,
                            budgetaryImpact=15,
                        ),
                        BudgetWindowAnnualImpact(
                            year="2027",
                            taxRevenueImpact=11,
                            federalTaxRevenueImpact=8,
                            stateTaxRevenueImpact=3,
                            benefitSpendingImpact=6,
                            budgetaryImpact=17,
                        ),
                    ],
                    totals=BudgetWindowTotals(
                        taxRevenueImpact=21,
                        federalTaxRevenueImpact=15,
                        stateTaxRevenueImpact=6,
                        benefitSpendingImpact=11,
                        budgetaryImpact=32,
                    ),
                ),
                error=None,
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:01+00:00",
                run_id="batch-run-123",
            )
        )

        response = client.get("/budget-window-jobs/mock-batch-job-id-123")

        assert response.status_code == 200
        assert response.json()["status"] == "complete"
        assert response.json()["result"]["totals"]["budgetaryImpact"] == 32
        assert response.json()["run_id"] == "batch-run-123"

    def test__given_non_integer_start_year__then_budget_window_submit_returns_422(
        self, mock_modal, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "year-2026",
                "window_size": 3,
            },
        )

        assert response.status_code == 422
        assert (
            response.json()["detail"][0]["msg"]
            == "Value error, start_year must be an integer year"
        )

    def test__given_end_year_past_2099__then_budget_window_submit_returns_422(
        self, mock_modal, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2099",
                "window_size": 2,
            },
        )

        assert response.status_code == 422
        assert (
            response.json()["detail"][0]["msg"]
            == "Value error, budget-window end_year must be 2099 or earlier"
        )

    def test__given_window_size_above_max__then_budget_window_submit_returns_422(
        self, mock_modal, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 76,
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "window_size"]

    def test__given_missing_region__then_budget_window_submit_returns_422(
        self, mock_modal, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "region"]

    def test__given_non_general_target__then_budget_window_submit_returns_422(
        self, mock_modal, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
                "target": "cliff",
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "target"]

    def test__given_max_parallel_above_active_limit__then_budget_window_submit_returns_422(
        self, mock_modal, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 3,
                "max_parallel": 21,
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "max_parallel"]

    def test__given_parent_call_raises__then_failure_persists_across_polls(
        self, mock_modal, client: TestClient
    ):
        """Regression test for #448: when the parent Modal FunctionCall raises
        on .get() the first poll flipped seed state to failed in-memory only.
        Subsequent polls re-read the unmodified seed and flipped back to
        "submitted". Verify the mutation is persisted so the terminal state
        survives a second poll."""

        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "policyengine-simulation-py4-10-0",
        }

        submit_response = client.post(
            "/simulate/economy/budget-window",
            json={
                "country": "us",
                "region": "us",
                "scope": "macro",
                "reform": {},
                "start_year": "2026",
                "window_size": 2,
                "max_parallel": 2,
            },
        )
        batch_id = submit_response.json()["batch_job_id"]

        # Arm the mocked call so .get(timeout=0) raises a non-timeout error.
        call = mock_modal["func"].last_call
        call.error = RuntimeError("worker crashed")
        call.running = False

        first_poll = client.get(f"/budget-window-jobs/{batch_id}")
        assert first_poll.status_code == 500
        first_body = first_poll.json()
        assert first_body["status"] == "failed"
        # Error is redacted (#453); the correlation id is in the string so
        # operators can jump from the user's report to the server-side log.
        assert first_body["error"].startswith("Simulation failed")
        assert "correlation_id=" in first_body["error"]
        assert "worker crashed" not in first_body["error"]

        second_poll = client.get(f"/budget-window-jobs/{batch_id}")
        assert second_poll.status_code == 500, second_poll.json()
        second_body = second_poll.json()
        assert second_body["status"] == "failed"
        assert second_body["error"].startswith("Simulation failed")
