"""Unit tests for gateway endpoints.

Tests verify the endpoint correctly resolves app names and lists the
versions it routes to.
"""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from fixtures.gateway_endpoints import TEST_ROUTING_STATE


class TestGetAppName:
    """Tests for the get_app_name helper function."""

    @pytest.mark.usefixtures("mock_modal")
    def test__given_no_version__then_prefers_latest_policyengine_bundle(self):
        from policyengine_simulation_gateway.endpoints import get_app_name

        app_name, resolved_version = get_app_name("us", None)

        assert resolved_version == "4.10.0"
        assert app_name == "policyengine-simulation-py4-10-0"

    @pytest.mark.usefixtures("mock_modal")
    def test__given_legacy_version_matching_policyengine_bundle__then_routes_by_bundle(
        self,
    ):
        from policyengine_simulation_gateway.endpoints import get_app_name

        app_name, resolved_version = get_app_name("uk", "4.10.0")

        assert resolved_version == "4.10.0"
        assert app_name == "policyengine-simulation-py4-10-0"

    @pytest.mark.usefixtures("mock_modal")
    def test__given_us_country_no_version__then_returns_latest_app(self):
        """Given 'us' and no version, when resolving, then use the 'latest' app."""
        from policyengine_simulation_gateway.endpoints import get_app_name

        # When
        app_name, resolved_version = get_app_name("us", None)

        # Then
        assert resolved_version == "4.10.0"
        assert app_name == "policyengine-simulation-py4-10-0"

    @pytest.mark.usefixtures("mock_modal")
    def test__given_us_country_with_version__then_returns_specified_app(self):
        """Given 'us' and a version, when resolving, then use that version's app."""
        from policyengine_simulation_gateway.endpoints import get_app_name

        # When
        app_name, resolved_version = get_app_name("us", "1.459.0")

        # Then
        assert resolved_version == "1.459.0"
        assert app_name == "policyengine-simulation-py3-9-0"

    @pytest.mark.usefixtures("mock_modal")
    def test__given_uk_country__then_uses_uk_version_dict(self):
        """Given country='uk', when resolving, then use the UK version dictionary."""
        from policyengine_simulation_gateway.endpoints import get_app_name

        # When
        app_name, resolved_version = get_app_name("uk", None)

        # Then
        assert resolved_version == "4.10.0"
        assert app_name == "policyengine-simulation-py4-10-0"

    def test__given_invalid_country__then_raises_value_error(self):
        """Given an invalid country code, when resolving, then raise ValueError."""
        from policyengine_simulation_gateway.endpoints import get_app_name

        # When / Then
        with pytest.raises(ValueError, match="Unknown country"):
            get_app_name("invalid", None)

    @pytest.mark.usefixtures("mock_modal")
    def test__given_unknown_version__then_raises_value_error(self):
        """Given an unregistered version, when resolving, then raise ValueError."""
        from policyengine_simulation_gateway.endpoints import get_app_name

        # When / Then
        with pytest.raises(ValueError, match="Unknown version"):
            get_app_name("us", "9.9.9")

    def test__given_active_state_absent__then_falls_back_to_legacy_country_dict(
        self, mock_modal
    ):
        from policyengine_simulation_gateway.endpoints import get_app_name

        del mock_modal["dicts"]["simulation-api-routing-state"]
        mock_modal["dicts"]["simulation-api-us-versions"] = {
            "latest": "1.500.0",
            "1.500.0": "legacy-app",
        }

        app_name, resolved_version = get_app_name("us", None)

        assert resolved_version == "1.500.0"
        assert app_name == "legacy-app"

    def test__given_only_legacy_country_routes__then_no_version_uses_country_latest(
        self, mock_modal
    ):
        from policyengine_simulation_gateway.endpoints import get_app_name

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

    @pytest.mark.usefixtures("mock_modal")
    def test__given_policyengine_version_field__then_routes_by_bundle(self):
        from policyengine_simulation_gateway.endpoints import resolve_route

        route = resolve_route("us", None, "4.10.0")

        assert route.response_version == "4.10.0"
        assert route.policyengine_version == "4.10.0"
        assert route.app_name == "policyengine-simulation-py4-10-0"


class TestVersionEndpoints:
    def test__given_bundle_with_dataset_sha__then_versions_report_identity(
        self, mock_modal, client: TestClient
    ):
        state = deepcopy(TEST_ROUTING_STATE)
        # The fixture's two bundles share one UK dict; give 4.10.0 its own.
        state["bundles"]["4.10.0"]["uk"] = {
            **state["bundles"]["4.10.0"]["uk"],
            "default_dataset_sha256": "a" * 64,
        }
        mock_modal["dicts"]["simulation-api-routing-state"] = {"active": state}

        response = client.get("/versions")

        assert response.status_code == 200
        bundles = response.json()["bundles"]
        assert bundles["4.10.0"]["uk"]["model_version"] == "2.66.0"
        assert bundles["4.10.0"]["uk"]["policyengine_version"] == "4.10.0"
        assert bundles["4.10.0"]["uk"]["dataset_sha256"] == "a" * 64
        assert bundles["4.10.0"]["uk"]["dataset"].startswith("hf://")
        # Legacy entries without a digest report none rather than a guess.
        assert bundles["3.9.0"]["uk"].get("dataset_sha256") is None
        assert bundles["4.10.0"]["us"]["model_version"] == "1.500.0"

    @pytest.mark.parametrize(
        "malformed",
        ["not-a-digest", "a" * 64 + "\n", "A" * 64, "a" * 63, 7],
        ids=["text", "trailing-newline", "uppercase", "short", "number"],
    )
    def test__given_malformed_dataset_sha__then_versions_omit_it(
        self, mock_modal, client: TestClient, malformed
    ):
        """#743 round-2 review: a digest with a trailing newline gave a 500."""
        state = deepcopy(TEST_ROUTING_STATE)
        state["bundles"]["4.10.0"]["uk"] = {
            **state["bundles"]["4.10.0"]["uk"],
            "default_dataset_sha256": malformed,
        }
        mock_modal["dicts"]["simulation-api-routing-state"] = {"active": state}

        response = client.get("/versions")

        assert response.status_code == 200
        assert response.json()["bundles"]["4.10.0"]["uk"].get("dataset_sha256") is None

    def test__given_bundle_with_dataset_sha__then_submission_names_it(
        self, mock_modal, client: TestClient
    ):
        state = deepcopy(TEST_ROUTING_STATE)
        state["bundles"]["4.10.0"]["uk"]["default_dataset_sha256"] = "b" * 64
        mock_modal["dicts"]["simulation-api-routing-state"] = {"active": state}

        response = client.post(
            "/simulate/economy/comparison",
            json={"country": "uk", "scope": "macro", "reform": {}},
        )

        assert response.status_code == 200
        bundle = response.json()["policyengine_bundle"]
        assert bundle["dataset_sha256"] == "b" * 64
        assert bundle["dataset"].startswith("hf://")

    @pytest.mark.usefixtures("mock_modal")
    def test__given_active_state__then_lists_policyengine_and_country_routes(
        self, client: TestClient
    ):
        response = client.get("/versions")

        assert response.status_code == 200
        data = response.json()
        assert data["policyengine"]["latest"] == "4.10.0"
        assert data["policyengine"]["4.10.0"] == "policyengine-simulation-py4-10-0"
        assert data["us"]["latest"] == "1.500.0"
        assert data["uk"]["latest"] == "2.66.0"

    @pytest.mark.usefixtures("mock_modal")
    def test__given_active_state__then_policyengine_versions_endpoint_works(
        self, client: TestClient
    ):
        response = client.get("/versions/policyengine")

        assert response.status_code == 200
        assert response.json()["latest"] == "4.10.0"

    @pytest.mark.usefixtures("mock_modal")
    def test__given_unknown_version_kind__then_404_without_exception(
        self, client: TestClient, monkeypatch
    ):
        recorded_errors = []
        monkeypatch.setattr(
            client.app.state.policyengine_observability,
            "record_exception",
            lambda exc, **kwargs: recorded_errors.append((exc, kwargs)),
        )

        response = client.get("/versions/fr")

        assert response.status_code == 404
        assert response.json()["detail"] == "Unknown version kind: fr"
        assert recorded_errors == []

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
            "spm_capabilities": {},
            "bundles": {},
            "published_at": {},
            "latest_since": None,
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
