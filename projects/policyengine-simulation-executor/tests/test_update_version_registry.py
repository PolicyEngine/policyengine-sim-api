"""Tests for the Modal routing-state publisher."""

from __future__ import annotations

import sys

import pytest

from src.modal.utils import update_version_registry as registry


class FakeDict:
    def __init__(self, initial: dict | None = None):
        self._data = dict(initial or {})

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        self._data[key] = value

    def get(self, key, default=None):
        return self._data.get(key, default)

    def items(self):
        return self._data.items()

    def snapshot(self) -> dict:
        return dict(self._data)


@pytest.fixture
def patched_modal(monkeypatch):
    stores: dict[str, FakeDict] = {}

    class _Dict:
        @staticmethod
        def from_name(
            name: str,
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

    monkeypatch.setattr(registry, "modal", _Modal)
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
        registry, "_country_bundle_metadata", fake_country_bundle_metadata
    )


def test__is_newer_version__advances_on_higher_minor():
    assert registry._is_newer_version("1.501.0", "1.500.0") is True


def test__is_newer_version__does_not_advance_on_lower_minor():
    assert registry._is_newer_version("1.499.0", "1.500.0") is False


def test__is_newer_version__advances_when_current_missing():
    assert registry._is_newer_version("1.500.0", None) is True


def test__is_newer_version__does_not_advance_on_equal():
    assert registry._is_newer_version("1.500.0", "1.500.0") is False


def test_validate_routing_state_accepts_complete_state(fake_bundle_metadata):
    state = registry.build_next_routing_state(
        current_state=None,
        app_name="policyengine-simulation-py4-19-1",
        policyengine_version="4.19.1",
        us_version="1.687.0",
        uk_version="2.88.14",
    )

    registry.validate_routing_state(state)


def test_validate_routing_state_rejects_missing_latest_route(fake_bundle_metadata):
    state = registry.build_next_routing_state(
        current_state=None,
        app_name="policyengine-simulation-py4-19-1",
        policyengine_version="4.19.1",
        us_version="1.687.0",
        uk_version="2.88.14",
    )
    state["routes"]["us"].pop("1.687.0")

    with pytest.raises(ValueError, match="latest.us"):
        registry.validate_routing_state(state)


def test_validate_routing_state_allows_migrated_policyengine_route_without_bundle(
    fake_bundle_metadata,
):
    state = registry.build_next_routing_state(
        current_state=None,
        app_name="policyengine-simulation-py4-19-1",
        policyengine_version="4.19.1",
        us_version="1.687.0",
        uk_version="2.88.14",
    )
    state["bundles"].pop("4.19.1")

    registry.validate_routing_state(state)


def test_validate_routing_state_rejects_required_policyengine_route_without_bundle(
    fake_bundle_metadata,
):
    state = registry.build_next_routing_state(
        current_state=None,
        app_name="policyengine-simulation-py4-19-1",
        policyengine_version="4.19.1",
        us_version="1.687.0",
        uk_version="2.88.14",
    )
    state["bundles"].pop("4.19.1")

    with pytest.raises(ValueError, match="no bundle manifest"):
        registry.validate_routing_state(
            state,
            required_policyengine_manifests=("4.19.1",),
        )


def test_build_next_routing_state_preserves_existing_routes(fake_bundle_metadata):
    current_state = registry.build_next_routing_state(
        current_state=None,
        app_name="policyengine-simulation-py4-18-0",
        policyengine_version="4.18.0",
        us_version="1.687.0",
        uk_version="2.88.14",
    )

    next_state = registry.build_next_routing_state(
        current_state=current_state,
        app_name="policyengine-simulation-py4-19-1",
        policyengine_version="4.19.1",
        us_version="1.687.0",
        uk_version="2.88.14",
    )

    assert (
        next_state["routes"]["policyengine"]["4.18.0"]
        == "policyengine-simulation-py4-18-0"
    )
    assert (
        next_state["routes"]["policyengine"]["4.19.1"]
        == "policyengine-simulation-py4-19-1"
    )
    assert next_state["latest"]["policyengine"] == "4.19.1"
    assert next_state["latest"]["us"] == "1.687.0"


def test_build_next_routing_state_keeps_existing_latest_when_incoming_older(
    fake_bundle_metadata,
):
    current_state = registry.build_next_routing_state(
        current_state=None,
        app_name="policyengine-simulation-py4-19-1",
        policyengine_version="4.19.1",
        us_version="1.687.0",
        uk_version="2.88.14",
    )

    next_state = registry.build_next_routing_state(
        current_state=current_state,
        app_name="policyengine-simulation-py4-18-0",
        policyengine_version="4.18.0",
        us_version="1.687.0",
        uk_version="2.88.14",
    )

    assert next_state["latest"]["policyengine"] == "4.19.1"
    assert (
        next_state["routes"]["policyengine"]["4.18.0"]
        == "policyengine-simulation-py4-18-0"
    )


def test_build_next_routing_state_force_latest_allows_downgrade(
    fake_bundle_metadata,
):
    current_state = registry.build_next_routing_state(
        current_state=None,
        app_name="policyengine-simulation-py4-19-1",
        policyengine_version="4.19.1",
        us_version="1.687.0",
        uk_version="2.88.14",
    )

    next_state = registry.build_next_routing_state(
        current_state=current_state,
        app_name="policyengine-simulation-py4-18-0",
        policyengine_version="4.18.0",
        us_version="1.687.0",
        uk_version="2.88.14",
        force_latest=True,
    )

    assert next_state["latest"]["policyengine"] == "4.18.0"


def test_build_next_routing_state_rejects_country_version_mismatch(
    fake_bundle_metadata,
):
    with pytest.raises(ValueError, match="US version"):
        registry.build_next_routing_state(
            current_state=None,
            app_name="policyengine-simulation-py4-19-1",
            policyengine_version="4.19.1",
            us_version="1.999.0",
            uk_version="2.88.14",
        )


def test_publish_routing_state_writes_only_active_snapshot(
    patched_modal,
    fake_bundle_metadata,
):
    registry.publish_routing_state(
        environment="main",
        app_name="policyengine-simulation-py4-19-1",
        policyengine_version="4.19.1",
        us_version="1.687.0",
        uk_version="2.88.14",
    )

    assert set(patched_modal) == {"main/simulation-api-routing-state"}
    snapshot = patched_modal["main/simulation-api-routing-state"].snapshot()
    active = snapshot["active"]
    assert (
        active["routes"]["policyengine"]["4.19.1"] == "policyengine-simulation-py4-19-1"
    )
    assert active["routes"]["us"]["1.687.0"] == "policyengine-simulation-py4-19-1"
    assert "dataset_aliases" not in active["bundles"]["4.19.1"]["us"]


def test_build_legacy_seed_routing_state_copies_legacy_routes_and_manifests():
    state = registry.build_legacy_seed_routing_state(
        policyengine_versions={
            "latest": "4.18.2",
            "4.18.2": "policyengine-simulation-py4-18-2",
            "4.13.1": "policyengine-simulation-py4-13-1",
        },
        us_versions={
            "latest": "1.729.0",
            "1.729.0": "policyengine-simulation-py4-18-2",
            "1.715.2": "policyengine-simulation-py4-13-1",
        },
        uk_versions={
            "latest": "2.89.2",
            "2.89.2": "policyengine-simulation-py4-18-2",
        },
        app_release_bundles={
            "4.18.2": {
                "app_name": "policyengine-simulation-py4-18-2",
                "policyengine_version": "4.18.2",
                "us": {"model_version": "1.729.0"},
                "uk": {"model_version": "2.89.2"},
            }
        },
    )

    assert state["latest"] == {
        "policyengine": "4.18.2",
        "us": "1.729.0",
        "uk": "2.89.2",
    }
    assert state["routes"]["us"]["1.715.2"] == "policyengine-simulation-py4-13-1"
    assert "4.18.2" in state["bundles"]
    assert "4.13.1" not in state["bundles"]


def test_build_legacy_seed_routing_state_infers_policyengine_routes_from_country_apps():
    state = registry.build_legacy_seed_routing_state(
        policyengine_versions={},
        us_versions={
            "latest": "1.729.0",
            "1.729.0": "policyengine-simulation-py4-18-2",
            "1.715.2": "policyengine-simulation-py4-13-1",
        },
        uk_versions={
            "latest": "2.89.2",
            "2.89.2": "policyengine-simulation-py4-18-2",
        },
    )

    assert (
        state["routes"]["policyengine"]["4.18.2"] == "policyengine-simulation-py4-18-2"
    )
    assert (
        state["routes"]["policyengine"]["4.13.1"] == "policyengine-simulation-py4-13-1"
    )
    assert state["latest"]["policyengine"] == "4.18.2"


def test_build_legacy_seed_routing_state_allows_country_only_legacy_routes():
    state = registry.build_legacy_seed_routing_state(
        policyengine_versions={},
        us_versions={
            "latest": "1.715.2",
            "1.715.2": "policyengine-simulation-us1-715-2-uk2-88-20",
        },
        uk_versions={
            "latest": "2.88.20",
            "2.88.20": "policyengine-simulation-us1-715-2-uk2-88-20",
        },
    )

    assert state["routes"]["policyengine"] == {}
    assert "policyengine" not in state["latest"]
    assert (
        state["routes"]["us"]["1.715.2"]
        == "policyengine-simulation-us1-715-2-uk2-88-20"
    )


def test_seed_active_routing_state_from_legacy_merges_existing_active(
    patched_modal,
    fake_bundle_metadata,
):
    patched_modal["main/simulation-api-policyengine-versions"] = FakeDict(
        {
            "latest": "4.18.2",
            "4.18.2": "policyengine-simulation-py4-18-2",
        }
    )
    patched_modal["main/simulation-api-us-versions"] = FakeDict(
        {
            "latest": "1.729.0",
            "1.729.0": "policyengine-simulation-py4-18-2",
        }
    )
    patched_modal["main/simulation-api-uk-versions"] = FakeDict(
        {
            "latest": "2.89.2",
            "2.89.2": "policyengine-simulation-py4-18-2",
        }
    )
    patched_modal["main/simulation-api-app-release-bundles"] = FakeDict()

    current_state = registry.build_next_routing_state(
        current_state=None,
        app_name="policyengine-simulation-py4-19-1",
        policyengine_version="4.19.1",
        us_version="1.687.0",
        uk_version="2.88.14",
    )
    patched_modal["main/simulation-api-routing-state"] = FakeDict(
        {"active": current_state}
    )

    state = registry.seed_active_routing_state_from_legacy(environment="main")

    assert (
        state["routes"]["policyengine"]["4.18.2"] == "policyengine-simulation-py4-18-2"
    )
    assert (
        state["routes"]["policyengine"]["4.19.1"] == "policyengine-simulation-py4-19-1"
    )
    assert state["latest"]["policyengine"] == "4.19.1"


def test_main_publishes_routing_state(
    patched_modal,
    fake_bundle_metadata,
    monkeypatch,
):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "update_version_registry",
            "--app-name",
            "policyengine-simulation-py4-19-1",
            "--policyengine-version",
            "4.19.1",
            "--us-version",
            "1.687.0",
            "--uk-version",
            "2.88.14",
            "--environment",
            "main",
        ],
    )

    registry.main()

    active = patched_modal["main/simulation-api-routing-state"].snapshot()["active"]
    assert active["latest"]["policyengine"] == "4.19.1"
    assert active["latest"]["us"] == "1.687.0"
    assert active["latest"]["uk"] == "2.88.14"
