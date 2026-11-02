"""Fixtures shared by the routing publisher's tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from registry_test_support import FakeDict
from src.modal.routing_publisher import bundles as routing_bundles
from src.modal.routing_publisher import stores as routing_stores

if TYPE_CHECKING:
    from src.modal.utils import update_version_registry as registry


@pytest.fixture
def serialized_workflow(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "PolicyEngine/policyengine-sim-api")
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        "PolicyEngine/policyengine-sim-api/.github/workflows/"
        "simulation-deploy.yml@refs/heads/main",
    )


@pytest.fixture
def patched_modal(monkeypatch, serialized_workflow):
    stores: dict[str, FakeDict] = {}

    class _Dict:
        @staticmethod
        def from_name(
            name: str,
            *,
            environment_name: str,
            create_if_missing: bool,
        ):
            key = f"{environment_name}/{name}"
            if key not in stores:
                if not create_if_missing:
                    raise KeyError(key)
                stores[key] = FakeDict()
            return stores[key]

    class _Modal:
        Dict = _Dict

    monkeypatch.setattr(routing_stores, "modal", _Modal)
    return stores


@pytest.fixture
def fake_bundle_metadata(monkeypatch):
    def fake_country_bundle_metadata(
        country: str,
    ) -> registry.CountryBundleMetadata:
        return {
            "country": country,
            "model_package_name": (
                "policyengine-us" if country == "us" else "policyengine-uk"
            ),
            "model_version": "1.687.0" if country == "us" else "2.88.14",
            "data_package_name": "populace-data",
            "data_package_version": "0.1.0",
            "data_version": (
                "populace-us-build" if country == "us" else "populace-uk-build"
            ),
            "data_artifact_revision": (
                "populace-us-build" if country == "us" else "populace-uk-build"
            ),
            "default_dataset": (
                "populace_us_2024" if country == "us" else "populace_uk_2023"
            ),
            "default_dataset_uri": f"hf://datasets/policyengine/{country}/default",
            "dataset_uris": {"default": f"hf://datasets/policyengine/{country}"},
            "dataset_repo_types": {"default": "dataset"},
        }

    monkeypatch.setattr(
        routing_bundles, "_country_bundle_metadata", fake_country_bundle_metadata
    )
