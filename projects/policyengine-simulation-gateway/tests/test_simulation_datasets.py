"""Tests for the dataset and bundle checks on a simulation submission."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from fixtures.gateway_endpoints import (
    TEST_APP_RELEASE_BUNDLE,
    TEST_ROUTING_STATE,
    resolve_test_dataset_uri,
)
from fixtures.gateway_responses import expected_bundle
from policyengine_simulation_contract.hf_dataset import HuggingFaceDatasetReferenceError


class TestSubmitSimulationEndpoint:
    """Tests for POST /simulate/economy/comparison endpoint."""

    def test__given_submission_without_dataset_name__then_bundle_uses_manifest_uri(
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
        }

        response = client.post("/simulate/economy/comparison", json=request_body)

        assert response.status_code == 200
        data = response.json()
        assert data["policyengine_bundle"]["dataset"] == resolve_test_dataset_uri(
            "us", None
        )
        assert "data" not in mock_modal["func"].last_payload

    def test__given_bundled_us_overlay__then_rejects_data_key(
        self, mock_modal, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "data": "populace_us_2024_acs_local",
            },
        )

        assert response.status_code == 422
        assert mock_modal["func"].last_payload is None

    def test__given_population_name_missing_from_selected_bundle__then_rejected(
        self, mock_modal, client: TestClient
    ):
        state = deepcopy(TEST_ROUTING_STATE)
        del state["bundles"]["4.10.0"]["us"]["dataset_uris"][
            "populace_us_2024_acs_local"
        ]
        mock_modal["dicts"]["simulation-api-routing-state"] = {"active": state}

        response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "data": "populace_us_2024_acs_local",
            },
        )

        assert response.status_code == 422
        assert mock_modal["func"].last_payload is None

    @pytest.mark.parametrize(
        ("country", "dataset"),
        [("us", "calibration_diagnostics"), ("uk", "local_authority_weights")],
    )
    def test__given_bundled_supporting_artifact__then_rejected_before_spawn(
        self, mock_modal, client: TestClient, country: str, dataset: str
    ):
        response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": country,
                "scope": "macro",
                "reform": {},
                "data": dataset,
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "extra_forbidden"
        assert mock_modal["func"].last_payload is None

    def test__given_unknown_dataset_field__then_rejected_at_public_boundary(
        self, mock_modal, client: TestClient
    ):
        response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "us",
                "scope": "macro",
                "reform": {},
                "dataset": "populace_us_2024",
            },
        )

        assert response.status_code == 422
        assert mock_modal["func"].last_payload is None

    def test__given_legacy_alias_in_bundle_snapshot__then_gateway_rejects_it(
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

        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "extra_forbidden"
        assert mock_modal["func"].last_payload is None

    def test__given_us_state_without_data__then_keeps_contract_and_uses_default_dataset(
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

    def test__given_submission_with_logical_revision__then_rejected_before_spawn(
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

        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "extra_forbidden"
        assert mock_modal["func"].last_payload is None

    def test__given_submission_with_explicit_uri_revision__then_rejected_before_spawn(
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

        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "extra_forbidden"
        assert mock_modal["func"].last_payload is None

    def test__given_submission_with_conflicting_data_versions__then_returns_422(
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

        assert response.status_code == 422
        assert len(response.json()["detail"]) == 2
        assert mock_modal["func"].last_payload is None

    def test__given_unmanaged_hf_reference__then_returns_422(
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
                "data": "hf://external/example-data/file.h5@does-not-exist",
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "extra_forbidden"
        assert mock_modal["func"].last_payload is None

    def test__given_uk_submission_without_data__then_bundle_dataset_is_versioned_uri(
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
        }

        response = client.post("/simulate/economy/comparison", json=request_body)

        assert response.status_code == 200
        data = response.json()
        assert data["policyengine_bundle"]["dataset"] == resolve_test_dataset_uri(
            "uk", "populace_uk_2023"
        )

    def test__given_policyengine_data_default__then_uses_package_storage_version(
        self,
        mock_modal,
        client: TestClient,
    ):
        app_name = "policyengine-simulation-py5-0-4"
        bundle = deepcopy(TEST_APP_RELEASE_BUNDLE)
        bundle["app_name"] = app_name
        bundle["policyengine_version"] = "5.0.4"
        bundle["uk"].update(
            {
                "model_version": "2.90.2",
                "data_package_name": "policyengine-uk-data",
                "data_package_version": "1.56.16",
                "data_version": "policyengine-uk-data-1.56.16",
                "data_artifact_revision": "1.56.16",
                "default_dataset": "enhanced_frs_2024_25",
                "default_dataset_uri": (
                    "hf://policyengine/policyengine-uk-data-private/"
                    "enhanced_frs_2024_25.h5@1.56.16"
                ),
                "dataset_uris": {
                    "enhanced_frs_2024_25": (
                        "hf://policyengine/policyengine-uk-data-private/"
                        "enhanced_frs_2024_25.h5@1.56.16"
                    ),
                },
            }
        )
        state = deepcopy(TEST_ROUTING_STATE)
        state["latest"] = {
            "policyengine": "5.0.4",
            "us": bundle["us"]["model_version"],
            "uk": "2.90.2",
        }
        state["routes"]["policyengine"]["5.0.4"] = app_name
        state["routes"]["uk"]["2.90.2"] = app_name
        state["bundles"]["5.0.4"] = bundle
        mock_modal["dicts"]["simulation-api-routing-state"] = {"active": state}

        response = client.post(
            "/simulate/economy/comparison",
            json={
                "country": "uk",
                "version": "2.90.2",
                "scope": "macro",
                "reform": {},
            },
        )

        assert response.status_code == 200
        response_bundle = response.json()["policyengine_bundle"]
        assert response_bundle["dataset"] == (
            "gs://policyengine-uk-data-private/enhanced_frs_2024_25.h5@1.56.16"
        )
        assert response_bundle["data_version"] == ("policyengine-uk-data-1.56.16")

    def test__given_uk_without_data_and_manifest_commit__then_uses_populace_default(
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
            msg = (
                "HF validation should not run for private GCS data: "
                f"{dataset_uri}@{revision}"
            )
            raise HuggingFaceDatasetReferenceError(msg)

        monkeypatch.setattr(
            "policyengine_simulation_contract.dataset_uri.with_hf_revision",
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

    def test__given_submission_with_runtime_bundle__then_rejects_revision_override(
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
            "_metadata": {"submission_claim_id": "process-123"},
        }

        response = client.post("/simulate/economy/comparison", json=request_body)

        assert response.status_code == 422
        assert len(response.json()["detail"]) == 2
        assert mock_modal["func"].last_payload is None

    def test__given_submission_with_unknown_dataset_name__then_rejected_before_spawn(
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

        assert response.status_code == 422
        assert response.json()["detail"][0]["type"] == "extra_forbidden"
        assert mock_modal["func"].last_payload is None
