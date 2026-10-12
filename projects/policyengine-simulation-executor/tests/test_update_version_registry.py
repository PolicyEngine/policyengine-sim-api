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

    def pop(self, key):
        return self._data.pop(key)

    def put(self, key, value, *, skip_if_exists=False):
        if skip_if_exists and key in self._data:
            return False
        self._data[key] = value
        return True

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


# --- Rollback: routing plans and restoring latest -----------------------------
#
# Invariants:
# - applying a release's plan and then restoring it returns the routing state
#   to exactly what it was (equal digests), whatever came before: no state, a
#   redeploy of the same version, an older release that does not move latest,
#   or legacy routes without manifests;
# - a plan is applied only onto its own starting state, and restored only
#   from its own published state; anything else is a conflict that changes
#   nothing, and restoring a plan that was never applied changes nothing;
# - restoring latest to a published version points latest and that bundle's
#   model pins at it and leaves every other route pinnable.

import json

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


def _manifests(monkeypatch, models: dict[str, tuple[str, str]]):
    def manifest(*, app_name, policyengine_version):
        us_version, uk_version = models[policyengine_version]
        return {
            "app_name": app_name,
            "policyengine_version": policyengine_version,
            "us": _country("us", us_version),
            "uk": {**_country("uk", uk_version), "default_dataset_sha256": "5" * 64},
        }

    monkeypatch.setattr(registry, "build_bundle_manifest_metadata", manifest)


def _publish(monkeypatch, state, policyengine_version, us_version, uk_version):
    _manifests(monkeypatch, {policyengine_version: (us_version, uk_version)})
    return registry.build_next_routing_state(
        current_state=state,
        app_name=_app(policyengine_version),
        policyengine_version=policyengine_version,
        us_version=us_version,
        uk_version=uk_version,
    )


def _plan(monkeypatch, state, version, us_version, uk_version, force=False):
    _manifests(monkeypatch, {version: (us_version, uk_version)})
    return registry.plan_routing_publication(
        current_state=state,
        app_name=_app(version),
        policyengine_version=version,
        us_version=us_version,
        uk_version=uk_version,
        force_latest=force,
    )


def _store(patched_modal, state):
    store = FakeDict({} if state is None else {"active": json.loads(json.dumps(state))})
    patched_modal["staging/simulation-api-routing-state"] = store
    return store


VERSIONS = [f"6.{minor}.0" for minor in range(5)]
RELEASES = st.lists(
    st.tuples(
        st.sampled_from(VERSIONS),
        st.sampled_from(["1.1.0", "1.2.0"]),
        st.sampled_from(["2.1.0", "2.2.0"]),
        st.booleans(),
    ),
    min_size=1,
    max_size=6,
)


@settings(max_examples=80, deadline=None)
@given(releases=RELEASES, legacy=st.booleans())
def test_publish_then_restore_returns_exactly_the_previous_state(releases, legacy):
    """#744 review: first, repeat and older releases are all undone exactly."""

    monkeypatch = pytest.MonkeyPatch()
    stores: dict = {}
    try:

        class _Dict:
            @staticmethod
            def from_name(name, environment_name, create_if_missing):
                key = f"{environment_name}/{name}"
                if key not in stores:
                    if not create_if_missing:
                        raise KeyError(key)
                    stores[key] = FakeDict()
                return stores[key]

        monkeypatch.setattr(registry, "modal", type("M", (), {"Dict": _Dict}))
        state = None
        if legacy:
            # A route seeded from the legacy dicts, with no manifest.
            state = registry._coerce_routing_state(None)
            state["routes"]["policyengine"]["5.9.0"] = _app("5.9.0")
            state["routes"]["uk"]["2.1.0"] = _app("5.9.0")
        for version, us, uk, force in releases[:-1]:
            state = _plan(monkeypatch, state, version, us, uk, force)["after"]
        version, us, uk, force = releases[-1]
        plan = _plan(monkeypatch, state, version, us, uk, force)
        if state is not None:
            stores["staging/simulation-api-routing-state"] = FakeDict(
                {"active": json.loads(json.dumps(state))}
            )
        before = registry.routing_state_digest(state)
        registry.apply_routing_plan(environment="staging", plan=plan)
        live = stores["staging/simulation-api-routing-state"]
        assert registry.routing_state_digest(live.get("active")) == plan["after_sha256"]
        outcome = registry.restore_routing_plan(environment="staging", plan=plan)
        assert registry.routing_state_digest(live.get("active")) == before
        assert outcome == (
            "unchanged" if plan["after_sha256"] == before else "restored"
        )
    finally:
        monkeypatch.undo()


def test_the_review_counterexample_restores_every_pin(monkeypatch, patched_modal):
    """Legacy 6.0/model1, then 6.1/model2, then a failed 6.2/model1."""

    state = registry._coerce_routing_state(None)
    state["routes"]["policyengine"]["6.0.0"] = _app("6.0.0")
    state["routes"]["uk"]["2.1.0"] = _app("6.0.0")
    state["latest"]["policyengine"] = "6.0.0"
    state = _publish(monkeypatch, state, "6.1.0", "1.1.0", "2.2.0")
    store = _store(patched_modal, state)
    plan = _plan(monkeypatch, state, "6.2.0", "1.1.0", "2.1.0")
    registry.apply_routing_plan(environment="staging", plan=plan)
    assert store.get("active")["routes"]["uk"]["2.1.0"] == _app("6.2.0")
    assert registry.restore_routing_plan(environment="staging", plan=plan) == "restored"
    assert store.get("active") == json.loads(json.dumps(state))
    assert store.get("active")["routes"]["uk"]["2.1.0"] == _app("6.0.0")


def test_a_first_publication_is_undone_by_removing_the_state(
    monkeypatch, patched_modal
):
    plan = _plan(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    registry.apply_routing_plan(environment="staging", plan=plan)
    store = patched_modal["staging/simulation-api-routing-state"]
    assert store.get("active") == plan["after"]
    assert registry.restore_routing_plan(environment="staging", plan=plan) == "restored"
    assert store.get("active") is None


def test_a_plan_that_was_never_applied_restores_nothing(monkeypatch, patched_modal):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    store = _store(patched_modal, state)
    plan = _plan(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    assert (
        registry.restore_routing_plan(environment="staging", plan=plan) == "unchanged"
    )
    assert store.get("active") == json.loads(json.dumps(state))


def test_a_plan_is_applied_only_onto_its_own_starting_state(monkeypatch, patched_modal):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    plan = _plan(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    moved = _publish(monkeypatch, state, "6.2.7", "1.1.0", "2.1.0")
    store = _store(patched_modal, moved)
    with pytest.raises(registry.RoutingConflict, match="nothing was published"):
        registry.apply_routing_plan(environment="staging", plan=plan)
    assert store.get("active") == json.loads(json.dumps(moved))


def test_a_restore_never_overwrites_a_later_change(monkeypatch, patched_modal):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    store = _store(patched_modal, state)
    plan = _plan(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    registry.apply_routing_plan(environment="staging", plan=plan)
    later = _publish(monkeypatch, store.get("active"), "6.4.0", "1.2.0", "2.2.0")
    store["active"] = later
    with pytest.raises(registry.RoutingConflict, match="nothing was restored"):
        registry.restore_routing_plan(environment="staging", plan=plan)
    assert store.get("active") == later


@pytest.mark.parametrize("side", ["before", "after"])
def test_a_tampered_plan_is_refused(monkeypatch, patched_modal, side):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    _store(patched_modal, state)
    plan = _plan(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    plan[side] = {**(plan[side] or {}), "generation": "edited"}
    with pytest.raises(ValueError, match=f"{side} state does not match"):
        registry.apply_routing_plan(environment="staging", plan=plan)
    with pytest.raises(ValueError, match=f"{side} state does not match"):
        registry.restore_routing_plan(environment="staging", plan=plan)


@pytest.mark.parametrize(
    ("version", "force", "advances"),
    [
        ("6.3.0", False, True),
        ("6.2.5", False, False),
        ("6.2.5", True, True),
        ("6.2.6", False, True),
    ],
    ids=["newer", "older", "forced", "redeploy"],
)
def test_plans_say_whether_latest_routes_to_the_release(
    monkeypatch, version, force, advances
):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    plan = _plan(monkeypatch, state, version, "1.1.0", "2.1.0", force)
    assert plan["advances_latest"] is advances


def test_restore_keeps_the_newer_release_pinnable(monkeypatch):
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


def _main(monkeypatch, *args):
    monkeypatch.setattr(
        sys, "argv", ["update_version_registry", "--environment", "staging", *args]
    )
    registry.main()


def test_main_plans_applies_and_restores(patched_modal, monkeypatch, capsys, tmp_path):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    store = _store(patched_modal, state)
    _manifests(monkeypatch, {"6.3.0": ("1.1.0", "2.2.0")})
    plan_file = tmp_path / "plan.json"
    _main(
        monkeypatch,
        "--app-name",
        _app("6.3.0"),
        "--policyengine-version",
        "6.3.0",
        "--us-version",
        "1.1.0",
        "--uk-version",
        "2.2.0",
        "--plan-out",
        str(plan_file),
    )
    plan = json.loads(plan_file.read_text())
    assert capsys.readouterr().out.splitlines() == [
        f"before_sha256={plan['before_sha256']}",
        f"after_sha256={plan['after_sha256']}",
        "advances_latest=true",
    ]
    assert store.get("active") == json.loads(
        json.dumps(state)
    )  # planning changes nothing
    _main(monkeypatch, "--apply-plan", str(plan_file))
    assert store.get("active")["latest"]["policyengine"] == "6.3.0"
    _main(monkeypatch, "--print-identity")
    assert capsys.readouterr().out.splitlines()[-4:] == [
        "policyengine_version=6.3.0",
        f"app_name={_app('6.3.0')}",
        "uk_model_version=2.2.0",
        "uk_dataset_sha256=" + "5" * 64,
    ]
    _main(monkeypatch, "--restore-plan", str(plan_file))
    assert "restored" in capsys.readouterr().out
    assert store.get("active") == json.loads(json.dumps(state))


def test_main_prints_and_restores_latest(patched_modal, monkeypatch, capsys):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    state = _publish(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    store = _store(patched_modal, state)
    _main(monkeypatch, "--print-latest")
    assert capsys.readouterr().out.strip() == "6.3.0"
    _main(monkeypatch, "--restore-latest", "6.2.6", "--expected-latest", "6.3.0")
    active = store.get("active")
    assert active["latest"]["policyengine"] == "6.2.6"
    assert active["routes"]["policyengine"]["6.3.0"] == _app("6.3.0")
    with pytest.raises(SystemExit):
        _main(monkeypatch, "--expected-latest", "6.2.6")


def test_latest_identity_of_an_empty_or_legacy_state_is_blank():
    assert registry.latest_release_identity(None) == {
        "policyengine_version": "",
        "app_name": "",
        "uk_model_version": "",
        "uk_dataset_sha256": "",
    }


# --- The routing write lock and multi-attempt restores --------------------------


def test_a_writer_cannot_change_routing_while_a_restore_holds_the_lock(
    monkeypatch, patched_modal
):
    """#744 review: a publisher between a restore's check and write is refused."""

    state = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = _store(patched_modal, state)
    plan = _plan(monkeypatch, state, "6.1.0", "1.1.0", "2.2.0")
    registry.apply_routing_plan(environment="staging", plan=plan)
    monkeypatch.setattr(registry, "ROUTING_LOCK_WAIT_SECONDS", 0)
    _manifests(monkeypatch, {"6.2.0": ("1.2.0", "2.2.0")})
    original_get = store.get
    interleaved = []

    def get(key, default=None):
        if key == "active" and not interleaved:
            interleaved.append(True)
            with pytest.raises(registry.RoutingLockHeld):
                registry.publish_routing_state(
                    environment="staging",
                    app_name=_app("6.2.0"),
                    policyengine_version="6.2.0",
                    us_version="1.2.0",
                    uk_version="2.2.0",
                )
        return original_get(key, default)

    monkeypatch.setattr(store, "get", get)
    assert registry.restore_routing_plan(environment="staging", plan=plan) == "restored"
    assert interleaved
    assert original_get("active") == json.loads(json.dumps(state))
    assert original_get(registry.ROUTING_WRITE_LOCK_KEY) is None


def test_the_lock_is_released_when_a_write_fails(monkeypatch, patched_modal):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    store = _store(patched_modal, state)
    plan = _plan(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    store["active"] = _publish(monkeypatch, state, "6.2.7", "1.1.0", "2.1.0")
    with pytest.raises(registry.RoutingConflict):
        registry.apply_routing_plan(environment="staging", plan=plan)
    assert store.get(registry.ROUTING_WRITE_LOCK_KEY) is None


def test_a_held_lock_waits_then_refuses_and_an_expired_one_is_broken():
    store = FakeDict()
    now = [1000.0]
    store.put(
        registry.ROUTING_WRITE_LOCK_KEY,
        {"owner": "x", "purpose": "other", "expires_at": now[0] + 60},
    )

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(registry.RoutingLockHeld, match="locked for 'other'"):
        with registry.routing_write_lock(
            store, purpose="me", wait_seconds=10, clock=lambda: now[0], sleep=sleep
        ):
            pass  # pragma: no cover
    assert store.get(registry.ROUTING_WRITE_LOCK_KEY)["owner"] == "x"
    now[0] += 100  # past the other writer's expiry
    with registry.routing_write_lock(
        store, purpose="me", wait_seconds=10, clock=lambda: now[0], sleep=sleep
    ) as owner:
        assert store.get(registry.ROUTING_WRITE_LOCK_KEY)["owner"] == owner
    assert store.get(registry.ROUTING_WRITE_LOCK_KEY) is None


def test_readers_ignore_the_lock_record(monkeypatch, patched_modal):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    store = _store(patched_modal, state)
    with registry.routing_write_lock(store, purpose="test"):
        assert registry.read_latest_policyengine_version(environment="staging") == (
            "6.2.6"
        )


def test_a_failed_retry_attempt_cannot_hide_the_publication(monkeypatch, patched_modal):
    """#744 review: attempt 1 published A to B; attempt 2 republished B."""

    a = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = _store(patched_modal, a)
    first = _plan(monkeypatch, a, "6.1.0", "1.1.0", "2.2.0")
    registry.apply_routing_plan(environment="staging", plan=first)
    second = _plan(monkeypatch, first["after"], "6.1.0", "1.1.0", "2.2.0")
    # Each publication is its own state, even of the same release.
    assert second["before_sha256"] != second["after_sha256"]
    registry.apply_routing_plan(environment="staging", plan=second)
    assert (
        registry.restore_routing_plans(environment="staging", plans=[first, second])
        == "restored"
    )
    assert store.get("active") == json.loads(json.dumps(a))


def test_an_old_plan_cannot_withdraw_a_healthy_republication(
    monkeypatch, patched_modal
):
    """#744 review: A to B failed and was restored; a healthy run republished B."""

    a = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = _store(patched_modal, a)
    failed = _plan(monkeypatch, a, "6.1.0", "1.1.0", "2.2.0")
    registry.apply_routing_plan(environment="staging", plan=failed)
    assert (
        registry.restore_routing_plan(environment="staging", plan=failed) == "restored"
    )
    healthy = _plan(monkeypatch, a, "6.1.0", "1.1.0", "2.2.0")
    registry.apply_routing_plan(environment="staging", plan=healthy)
    with pytest.raises(registry.RoutingConflict, match="nothing was restored"):
        registry.restore_routing_plan(environment="staging", plan=failed)
    assert store.get("active") == healthy["after"]


def test_a_chain_of_attempts_is_undone_in_order(monkeypatch, patched_modal):
    a = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = _store(patched_modal, a)
    first = _plan(monkeypatch, a, "6.1.0", "1.1.0", "2.2.0")
    registry.apply_routing_plan(environment="staging", plan=first)
    second = _plan(monkeypatch, first["after"], "6.1.1", "1.2.0", "2.2.0")
    registry.apply_routing_plan(environment="staging", plan=second)
    assert (
        registry.restore_routing_plans(environment="staging", plans=[first, second])
        == "restored"
    )
    assert store.get("active") == json.loads(json.dumps(a))
    # Done again, nothing is left to undo.
    assert (
        registry.restore_routing_plans(environment="staging", plans=[first, second])
        == "unchanged"
    )


def test_main_restores_several_plans(patched_modal, monkeypatch, capsys, tmp_path):
    a = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = _store(patched_modal, a)
    first = _plan(monkeypatch, a, "6.1.0", "1.1.0", "2.2.0")
    registry.apply_routing_plan(environment="staging", plan=first)
    second = _plan(monkeypatch, first["after"], "6.1.0", "1.1.0", "2.2.0")
    files = []
    for index, plan in enumerate((first, second), start=1):
        path = tmp_path / f"plan-{index}.json"
        path.write_text(json.dumps(plan))
        files.append(str(path))
    _main(monkeypatch, "--restore-plan", *files)
    assert "restored" in capsys.readouterr().out
    assert store.get("active") == json.loads(json.dumps(a))


def test_a_waiter_never_removes_a_lock_taken_after_a_release():
    """#744 review: an absent lock is retried by insertion, never deleted."""

    class Racing(FakeDict):
        def __init__(self):
            super().__init__()
            self.puts = 0
            self.gets = 0

        def put(self, key, value, *, skip_if_exists=False):
            if key == registry.ROUTING_WRITE_LOCK_KEY and self.puts == 0:
                self.puts += 1
                return False  # the previous holder still had it
            return super().put(key, value, skip_if_exists=skip_if_exists)

        def get(self, key, default=None):
            if key == registry.ROUTING_WRITE_LOCK_KEY and self.gets == 0:
                self.gets += 1
                # The previous holder released; another publisher takes it at
                # once, after this read.
                answer = super().get(key, default)
                super().put(
                    key,
                    {"owner": "publisher", "purpose": "publish", "expires_at": 1e12},
                )
                return answer
            return super().get(key, default)

    store = Racing()
    now = [1000.0]
    with pytest.raises(registry.RoutingLockHeld):
        with registry.routing_write_lock(
            store,
            purpose="restore",
            wait_seconds=10,
            clock=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        ):
            pass  # pragma: no cover
    assert store.get(registry.ROUTING_WRITE_LOCK_KEY)["owner"] == "publisher"


def test_only_the_reclaim_winner_removes_an_expired_lock():
    store = FakeDict()
    expired = {"owner": "dead", "purpose": "x", "expires_at": 0}
    store.put(registry.ROUTING_WRITE_LOCK_KEY, expired)
    store.put(f"{registry.ROUTING_WRITE_LOCK_KEY}:reclaim:dead", {"at": 1})
    registry._reclaim_expired_lock(store, expired)  # another waiter is reclaiming
    assert store.get(registry.ROUTING_WRITE_LOCK_KEY) == expired
    store.pop(f"{registry.ROUTING_WRITE_LOCK_KEY}:reclaim:dead")
    registry._reclaim_expired_lock(store, expired)
    assert store.get(registry.ROUTING_WRITE_LOCK_KEY) is None
    assert store.get(f"{registry.ROUTING_WRITE_LOCK_KEY}:reclaim:dead") is None


def test_reclaiming_puts_back_a_lock_that_changed_hands():
    store = FakeDict()
    store.put(registry.ROUTING_WRITE_LOCK_KEY, {"owner": "fresh", "expires_at": 1e12})
    registry._reclaim_expired_lock(store, {"owner": "dead", "expires_at": 0})
    assert store.get(registry.ROUTING_WRITE_LOCK_KEY)["owner"] == "fresh"


def test_release_never_removes_someone_elses_lock():
    store = FakeDict()
    with registry.routing_write_lock(store, purpose="mine"):
        store[registry.ROUTING_WRITE_LOCK_KEY] = {"owner": "other", "expires_at": 1e12}
    assert store.get(registry.ROUTING_WRITE_LOCK_KEY)["owner"] == "other"


def test_apply_waits_for_and_then_refuses_a_held_lock(monkeypatch, patched_modal):
    """#744 review: removing the apply lock must fail a test."""

    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    store = _store(patched_modal, state)
    plan = _plan(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    store.put(
        registry.ROUTING_WRITE_LOCK_KEY,
        {"owner": "other", "purpose": "restore", "expires_at": 1e12},
    )
    with pytest.raises(registry.RoutingLockHeld):
        registry.apply_routing_plan(
            environment="staging", plan=plan, lock_wait_seconds=0
        )
    assert store.get("active") == json.loads(json.dumps(state))


def test_plans_carry_their_publication_id(monkeypatch):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    _manifests(monkeypatch, {"6.3.0": ("1.1.0", "2.2.0")})
    plan = registry.plan_routing_publication(
        current_state=state,
        app_name=_app("6.3.0"),
        policyengine_version="6.3.0",
        us_version="1.1.0",
        uk_version="2.2.0",
        publication_id="run-7-attempt-2-prod",
    )
    assert plan["publication_id"] == "run-7-attempt-2-prod"
    assert plan["after"]["generation"] == f"6.3.0:{_app('6.3.0')}:run-7-attempt-2-prod"
    registry.validate_routing_plan(plan)
