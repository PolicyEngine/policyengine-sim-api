"""Tests for the Modal routing-state publisher."""

from __future__ import annotations

import sys

import pytest

from src.modal.utils import update_version_registry as registry


class _StubForecast:
    years = (2026,)

    def entry(self, year, *, scenario, as_of):
        return {"year": year, "scenario": scenario, "as_of": as_of}


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


def test_bundle_manifest_records_policyengine_6_spm_capability(
    fake_bundle_metadata,
    monkeypatch,
):
    from policyengine.bundle import get_current_bundle
    from policyengine_simulation_contract.spm import SPM_CONTRACT_VERSION

    from policyengine_simulation_executor import spm as executor_spm

    monkeypatch.setattr(
        executor_spm,
        "_forecast",
        lambda expected_sha256: _StubForecast(),
    )

    bundle = get_current_bundle()
    metadata = registry.build_bundle_manifest_metadata(
        app_name="policyengine-simulation-py6-1-2",
        policyengine_version=bundle["policyengine_version"],
    )

    assert metadata["spm"] == {
        "contract_version": SPM_CONTRACT_VERSION,
        "defaults": bundle["measurements"]["spm"],
    }


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


def test_country_bundle_metadata_records_the_certified_dataset_sha256(monkeypatch):
    from types import SimpleNamespace

    bundle = SimpleNamespace(
        country="uk",
        model_package_name="policyengine-uk",
        model_version="2.126.8",
        data_package_name="policyengine-uk-data",
        data_package_version="1.58.0",
        data_version="policyengine-uk-data-1.58.0",
        data_artifact_revision="1.58.0",
        default_dataset="enhanced_frs_2024_25",
        default_dataset_uri="hf://policyengine/policyengine-uk-data-private/enhanced_frs_2024_25.h5@1.58.0",
        dataset_uris={},
        dataset_repo_types={},
        default_dataset_sha256="a" * 64,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.release_bundle.get_country_release_bundle",
        lambda country: bundle,
    )

    metadata = registry._country_bundle_metadata("uk")

    assert metadata["default_dataset_sha256"] == "a" * 64


# --- Rollback: restoring latest and withdrawing a failed release -------------
#
# Invariants:
# - publishing release R and then restoring the previous latest with R
#   withdrawn returns routes, bundles and latest to their state before R;
# - after any restore, latest and every route name an app that still has a
#   policyengine route and manifest (validate_routing_state holds), and no
#   route reaches a withdrawn app;
# - a restore conditioned on expected_latest never changes a state whose
#   latest is something else.

from hypothesis import given, settings
from hypothesis import strategies as st


def _country(country: str, model_version: str) -> registry.CountryBundleMetadata:
    return {
        "country": country,
        "model_package_name": f"policyengine-{country}",
        "model_version": model_version,
        "data_package_name": "populace-data",
        "data_package_version": "0.1.0",
        "data_version": f"populace-{country}-build",
        "data_artifact_revision": f"populace-{country}-build",
        "default_dataset": f"populace_{country}",
        "default_dataset_uri": f"hf://datasets/policyengine/{country}/default",
        "dataset_uris": {},
        "dataset_repo_types": {},
    }


def _app(policyengine_version: str) -> str:
    return "policyengine-simulation-py" + policyengine_version.replace(".", "-")


def _publish(monkeypatch, state, policyengine_version, us_version, uk_version):
    def manifest(*, app_name, policyengine_version):
        return {
            "app_name": app_name,
            "policyengine_version": policyengine_version,
            "us": _country("us", us_version),
            "uk": _country("uk", uk_version),
        }

    monkeypatch.setattr(registry, "build_bundle_manifest_metadata", manifest)
    return registry.build_next_routing_state(
        current_state=state,
        app_name=_app(policyengine_version),
        policyengine_version=policyengine_version,
        us_version=us_version,
        uk_version=uk_version,
    )


def _without_generation(state):
    return {key: value for key, value in state.items() if key != "generation"}


RELEASES = st.lists(
    st.tuples(st.sampled_from(["1.1.0", "1.2.0"]), st.sampled_from(["2.1.0", "2.2.0"])),
    min_size=2,
    max_size=6,
)


@settings(max_examples=60, deadline=None)
@given(releases=RELEASES)
def test_publish_then_rollback_returns_the_previous_routing(releases):
    monkeypatch = pytest.MonkeyPatch()
    try:
        state = None
        for index, (us, uk) in enumerate(releases[:-1]):
            state = _publish(monkeypatch, state, f"6.{index}.0", us, uk)
        before = registry._coerce_routing_state(state)
        failed = f"6.{len(releases) - 1}.0"
        after = _publish(monkeypatch, state, failed, *releases[-1])
        restored = registry.build_restored_routing_state(
            current_state=after,
            policyengine_version=before["latest"]["policyengine"],
            expected_latest=failed,
            withdraw_version=failed,
        )
    finally:
        monkeypatch.undo()
    assert _without_generation(restored) == _without_generation(before)
    apps = set(restored["routes"]["policyengine"].values())
    for country in ("us", "uk"):
        assert set(restored["routes"][country].values()) <= apps
    assert _app(failed) not in apps


def test_restore_without_withdrawal_keeps_the_newer_release_pinnable(monkeypatch):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    state = _publish(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    restored = registry.build_restored_routing_state(
        current_state=state, policyengine_version="6.2.6"
    )
    assert restored["latest"] == {"policyengine": "6.2.6", "us": "1.1.0", "uk": "2.1.0"}
    assert restored["routes"]["policyengine"]["6.3.0"] == _app("6.3.0")
    assert restored["routes"]["uk"] == {"2.1.0": _app("6.2.6"), "2.2.0": _app("6.3.0")}
    assert restored["generation"] == f"rollback:6.2.6:{_app('6.2.6')}"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"policyengine_version": "6.1.0"}, "no published route"),
        (
            {"policyengine_version": "6.2.6", "expected_latest": "6.2.6"},
            "another release changed routing",
        ),
        (
            {"policyengine_version": "6.2.6", "withdraw_version": "6.2.6"},
            "same version",
        ),
        (
            {"policyengine_version": "6.2.6", "withdraw_version": "6.9.9"},
            "no route to withdraw",
        ),
    ],
)
def test_unsafe_restores_are_refused(monkeypatch, kwargs, message):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    state = _publish(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    with pytest.raises(ValueError, match=message):
        registry.build_restored_routing_state(current_state=state, **kwargs)


def test_a_restore_needs_a_manifest_that_matches_its_route(monkeypatch):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    state = _publish(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    state["bundles"]["6.2.6"]["app_name"] = "someone-else"
    with pytest.raises(ValueError, match="manifest names app"):
        registry.build_restored_routing_state(
            current_state=state, policyengine_version="6.2.6"
        )


def test_main_prints_and_restores_latest(patched_modal, monkeypatch, capsys):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    state = _publish(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    patched_modal["staging/simulation-api-routing-state"] = FakeDict({"active": state})

    monkeypatch.setattr(
        sys,
        "argv",
        ["update_version_registry", "--environment", "staging", "--print-latest"],
    )
    registry.main()
    assert capsys.readouterr().out.strip() == "6.3.0"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "update_version_registry",
            "--environment",
            "staging",
            "--restore-latest",
            "6.2.6",
            "--expected-latest",
            "6.3.0",
            "--withdraw",
            "6.3.0",
        ],
    )
    registry.main()
    active = patched_modal["staging/simulation-api-routing-state"].snapshot()["active"]
    assert active["latest"]["policyengine"] == "6.2.6"
    assert "6.3.0" not in active["routes"]["policyengine"]
    assert active["routes"]["uk"] == {"2.1.0": _app("6.2.6")}

    monkeypatch.setattr(
        sys,
        "argv",
        ["update_version_registry", "--environment", "staging", "--withdraw", "6.2.6"],
    )
    with pytest.raises(SystemExit):
        registry.main()
