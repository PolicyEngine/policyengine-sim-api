"""Tests for the routing state's validation and next-state rules."""

from __future__ import annotations

import re

import pytest
from src.modal.routing_publisher import state as routing_state
from src.modal.utils import update_version_registry as registry


def test__is_newer_version__advances_on_higher_minor():
    assert routing_state.is_newer_version("1.501.0", "1.500.0") is True


def test__is_newer_version__does_not_advance_on_lower_minor():
    assert routing_state.is_newer_version("1.499.0", "1.500.0") is False


def test__is_newer_version__advances_when_current_missing():
    assert routing_state.is_newer_version("1.500.0", None) is True


def test__is_newer_version__does_not_advance_on_equal():
    assert routing_state.is_newer_version("1.500.0", "1.500.0") is False


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_validate_routing_state_accepts_complete_state():
    state = registry.build_next_routing_state(
        current_state=None,
        deployed=registry.DeployedAppVersions(
            app_name="policyengine-simulation-py4-19-1",
            policyengine_version="4.19.1",
            us_version="1.687.0",
            uk_version="2.88.14",
        ),
    )

    registry.validate_routing_state(state)


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_validate_routing_state_rejects_missing_latest_route():
    state = registry.build_next_routing_state(
        current_state=None,
        deployed=registry.DeployedAppVersions(
            app_name="policyengine-simulation-py4-19-1",
            policyengine_version="4.19.1",
            us_version="1.687.0",
            uk_version="2.88.14",
        ),
    )
    state["routes"]["us"].pop("1.687.0")

    with pytest.raises(ValueError, match=r"latest\.us"):
        registry.validate_routing_state(state)


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_validate_routing_state_allows_migrated_policyengine_route_without_bundle():
    state = registry.build_next_routing_state(
        current_state=None,
        deployed=registry.DeployedAppVersions(
            app_name="policyengine-simulation-py4-19-1",
            policyengine_version="4.19.1",
            us_version="1.687.0",
            uk_version="2.88.14",
        ),
    )
    state["bundles"].pop("4.19.1")

    registry.validate_routing_state(state)


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_validate_routing_state_rejects_required_policyengine_route_without_bundle():
    state = registry.build_next_routing_state(
        current_state=None,
        deployed=registry.DeployedAppVersions(
            app_name="policyengine-simulation-py4-19-1",
            policyengine_version="4.19.1",
            us_version="1.687.0",
            uk_version="2.88.14",
        ),
    )
    state["bundles"].pop("4.19.1")

    with pytest.raises(ValueError, match="no bundle manifest"):
        registry.validate_routing_state(
            state,
            required_policyengine_manifests=("4.19.1",),
        )


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_build_next_routing_state_preserves_existing_routes():
    current_state = registry.build_next_routing_state(
        current_state=None,
        deployed=registry.DeployedAppVersions(
            app_name="policyengine-simulation-py4-18-0",
            policyengine_version="4.18.0",
            us_version="1.687.0",
            uk_version="2.88.14",
        ),
    )

    next_state = registry.build_next_routing_state(
        current_state=current_state,
        deployed=registry.DeployedAppVersions(
            app_name="policyengine-simulation-py4-19-1",
            policyengine_version="4.19.1",
            us_version="1.687.0",
            uk_version="2.88.14",
        ),
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


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_build_next_routing_state_keeps_existing_latest_when_incoming_older():
    current_state = registry.build_next_routing_state(
        current_state=None,
        deployed=registry.DeployedAppVersions(
            app_name="policyengine-simulation-py4-19-1",
            policyengine_version="4.19.1",
            us_version="1.687.0",
            uk_version="2.88.14",
        ),
    )

    next_state = registry.build_next_routing_state(
        current_state=current_state,
        deployed=registry.DeployedAppVersions(
            app_name="policyengine-simulation-py4-18-0",
            policyengine_version="4.18.0",
            us_version="1.687.0",
            uk_version="2.88.14",
        ),
    )

    assert next_state["latest"]["policyengine"] == "4.19.1"
    assert (
        next_state["routes"]["policyengine"]["4.18.0"]
        == "policyengine-simulation-py4-18-0"
    )


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_build_next_routing_state_force_latest_allows_downgrade():
    current_state = registry.build_next_routing_state(
        current_state=None,
        deployed=registry.DeployedAppVersions(
            app_name="policyengine-simulation-py4-19-1",
            policyengine_version="4.19.1",
            us_version="1.687.0",
            uk_version="2.88.14",
        ),
    )

    next_state = registry.build_next_routing_state(
        current_state=current_state,
        deployed=registry.DeployedAppVersions(
            app_name="policyengine-simulation-py4-18-0",
            policyengine_version="4.18.0",
            us_version="1.687.0",
            uk_version="2.88.14",
        ),
        force_latest=True,
    )

    assert next_state["latest"]["policyengine"] == "4.18.0"


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_build_next_routing_state_rejects_country_version_mismatch():
    with pytest.raises(ValueError, match="US version"):
        registry.build_next_routing_state(
            current_state=None,
            deployed=registry.DeployedAppVersions(
                app_name="policyengine-simulation-py4-19-1",
                policyengine_version="4.19.1",
                us_version="1.999.0",
                uk_version="2.88.14",
            ),
        )


def test_latest_identity_of_an_empty_or_legacy_state_is_blank():
    assert registry.latest_release_identity(None) == {
        "policyengine_version": "",
        "app_name": "",
        "uk_model_version": "",
        "uk_dataset_sha256": "",
    }


def _routing_state(**overrides):
    state = {
        "schema_version": 1,
        "generation": "4.19.1:app-4-19-1",
        "latest": {"policyengine": "4.19.1", "us": "1.687.0", "uk": "2.88.14"},
        "routes": {
            "policyengine": {"4.19.1": "app-4-19-1"},
            "us": {"1.687.0": "app-4-19-1"},
            "uk": {"2.88.14": "app-4-19-1"},
        },
        "bundles": {
            "4.19.1": {"app_name": "app-4-19-1", "policyengine_version": "4.19.1"}
        },
    }
    for path, value in overrides.items():
        section, _, key = path.partition(".")
        if key:
            state[section] = {**state[section], key: value}
        else:
            state[section] = value
    return state


@pytest.mark.parametrize(
    ("overrides", "required", "message"),
    [
        ({"schema_version": 2}, (), "Routing state schema_version must be 1"),
        ({"latest.us": ""}, (), "Routing state latest.us is missing"),
        (
            {"latest.uk": "9.9.9"},
            (),
            "Routing state latest.uk='9.9.9' has no route",
        ),
        (
            {"latest.policyengine": None},
            (),
            "Routing state latest.policyengine is missing",
        ),
        (
            {"routes.policyengine": {}, "latest.policyengine": None},
            ("4.19.1",),
            "Routing state latest.policyengine is missing",
        ),
        (
            {"bundles": {}},
            ("4.19.1",),
            "Routing state policyengine route '4.19.1' has no bundle manifest",
        ),
        (
            {
                "bundles": {
                    "4.19.1": {"app_name": "app-4-19-1", "policyengine_version": "4"}
                }
            },
            (),
            "Routing state bundle manifest key does not match policyengine_version "
            "for '4.19.1'",
        ),
        (
            {
                "bundles": {
                    "4.19.1": {"app_name": "other", "policyengine_version": "4.19.1"}
                }
            },
            (),
            "Routing state bundle manifest app_name does not match route for '4.19.1'",
        ),
        (
            {"latest.us": "", "bundles": {}},
            ("4.19.1",),
            "Routing state latest.us is missing",
        ),
    ],
)
def test_validate_routing_state_reports_the_first_inconsistency(
    overrides, required, message
):
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        registry.validate_routing_state(
            _routing_state(**overrides),
            required_policyengine_manifests=required,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"bundles": {}},
        {"bundles": {"4.19.1": "not-a-manifest"}},
        {"routes.policyengine": {}, "latest.policyengine": None, "bundles": {}},
    ],
)
def test_validate_routing_state_accepts_consistent_states(overrides):
    registry.validate_routing_state(_routing_state(**overrides))
