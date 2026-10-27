"""Tests for publishing a deployed app and seeding from the legacy Dicts."""

from __future__ import annotations

import sys

import pytest
from registry_test_support import FakeDict
from src.modal.utils import update_version_registry as registry


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_publish_routing_state_writes_only_active_snapshot(
    patched_modal,
):
    registry.publish_routing_state(
        environment="main",
        deployed=registry.DeployedAppVersions(
            app_name="policyengine-simulation-py4-19-1",
            policyengine_version="4.19.1",
            us_version="1.687.0",
            uk_version="2.88.14",
        ),
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


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_seed_active_routing_state_from_legacy_merges_existing_active(
    patched_modal,
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
        deployed=registry.DeployedAppVersions(
            app_name="policyengine-simulation-py4-19-1",
            policyengine_version="4.19.1",
            us_version="1.687.0",
            uk_version="2.88.14",
        ),
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


@pytest.mark.usefixtures("fake_bundle_metadata", "serialized_workflow")
def test_main_publishes_routing_state(
    patched_modal,
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
