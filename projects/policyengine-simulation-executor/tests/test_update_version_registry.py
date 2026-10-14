"""Tests for the Modal routing-state publisher."""

from __future__ import annotations

import json
import re
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
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

    def snapshot(self) -> dict:
        return dict(self._data)


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


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_validate_routing_state_accepts_complete_state():
    state = registry.build_next_routing_state(
        current_state=None,
        app_name="policyengine-simulation-py4-19-1",
        policyengine_version="4.19.1",
        us_version="1.687.0",
        uk_version="2.88.14",
    )

    registry.validate_routing_state(state)


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_bundle_manifest_records_policyengine_6_spm_capability(
    monkeypatch,
):
    from policyengine.bundle import get_current_bundle
    from policyengine_simulation_contract.spm import SPM_CONTRACT_VERSION
    from policyengine_simulation_executor import spm as executor_spm

    monkeypatch.setattr(
        executor_spm,
        "_forecast",
        lambda _expected_sha256: _StubForecast(),
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


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_bundle_manifest_declares_job_progress_capability(monkeypatch):
    from policyengine_simulation_contract.job_progress import (
        JOB_PROGRESS_CAPABILITY_VERSION,
    )
    from policyengine_simulation_executor import spm as executor_spm

    monkeypatch.setattr(executor_spm, "runtime_spm_capability", lambda: None)

    metadata = registry.build_bundle_manifest_metadata(
        app_name="policyengine-simulation-py6-2-7",
        policyengine_version="6.2.7",
    )

    # The gateway reports a queued stage only for workers that declare this.
    assert metadata["job_progress"] == JOB_PROGRESS_CAPABILITY_VERSION


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_validate_routing_state_rejects_missing_latest_route():
    state = registry.build_next_routing_state(
        current_state=None,
        app_name="policyengine-simulation-py4-19-1",
        policyengine_version="4.19.1",
        us_version="1.687.0",
        uk_version="2.88.14",
    )
    state["routes"]["us"].pop("1.687.0")

    with pytest.raises(ValueError, match=r"latest\.us"):
        registry.validate_routing_state(state)


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_validate_routing_state_allows_migrated_policyengine_route_without_bundle():
    state = registry.build_next_routing_state(
        current_state=None,
        app_name="policyengine-simulation-py4-19-1",
        policyengine_version="4.19.1",
        us_version="1.687.0",
        uk_version="2.88.14",
    )
    state["bundles"].pop("4.19.1")

    registry.validate_routing_state(state)


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_validate_routing_state_rejects_required_policyengine_route_without_bundle():
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


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_build_next_routing_state_preserves_existing_routes():
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


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_build_next_routing_state_keeps_existing_latest_when_incoming_older():
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


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_build_next_routing_state_force_latest_allows_downgrade():
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


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_build_next_routing_state_rejects_country_version_mismatch():
    with pytest.raises(ValueError, match="US version"):
        registry.build_next_routing_state(
            current_state=None,
            app_name="policyengine-simulation-py4-19-1",
            policyengine_version="4.19.1",
            us_version="1.999.0",
            uk_version="2.88.14",
        )


@pytest.mark.usefixtures("fake_bundle_metadata")
def test_publish_routing_state_writes_only_active_snapshot(
    patched_modal,
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
        lambda _country: bundle,
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


def _plan(
    monkeypatch,
    state,
    version,
    us_version,
    uk_version,
    *,
    force=False,
    publication_id=None,
):
    _manifests(monkeypatch, {version: (us_version, uk_version)})
    return registry.plan_routing_publication(
        current_state=state,
        app_name=_app(version),
        policyengine_version=version,
        us_version=us_version,
        uk_version=uk_version,
        force_latest=force,
        publication_id=publication_id,
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
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_REPOSITORY", "PolicyEngine/policyengine-sim-api")
        monkeypatch.setenv(
            "GITHUB_WORKFLOW_REF",
            "PolicyEngine/policyengine-sim-api/.github/workflows/"
            "simulation-deploy.yml@refs/heads/main",
        )

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
            state = _plan(monkeypatch, state, version, us, uk, force=force)["after"]
        version, us, uk, force = releases[-1]
        plan = _plan(monkeypatch, state, version, us, uk, force=force)
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
    with pytest.raises(registry.RoutingConflictError, match="nothing was published"):
        registry.apply_routing_plan(environment="staging", plan=plan)
    assert store.get("active") == json.loads(json.dumps(moved))


def test_a_restore_never_overwrites_a_later_change(monkeypatch, patched_modal):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    store = _store(patched_modal, state)
    plan = _plan(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")
    registry.apply_routing_plan(environment="staging", plan=plan)
    later = _publish(monkeypatch, store.get("active"), "6.4.0", "1.2.0", "2.2.0")
    store["active"] = later
    with pytest.raises(registry.RoutingConflictError, match="nothing was restored"):
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
    plan = _plan(monkeypatch, state, version, "1.1.0", "2.1.0", force=force)
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


@pytest.mark.usefixtures("serialized_workflow")
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


@pytest.mark.usefixtures("serialized_workflow")
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
    with pytest.raises(registry.RoutingConflictError, match="nothing was restored"):
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


@pytest.mark.usefixtures("serialized_workflow")
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


@pytest.mark.parametrize(
    "workflow",
    ["simulation-deploy.yml", "stage12-deploy.yml", "simulation-rollback.yml"],
)
@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/tags/v6.3.0", "a" * 40])
@pytest.mark.usefixtures("serialized_workflow")
def test_routing_guard_accepts_the_serialized_caller_workflows(
    monkeypatch, workflow, ref
):
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        f"PolicyEngine/policyengine-sim-api/.github/workflows/{workflow}@{ref}",
    )
    registry._require_serialized_routing_workflow()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("GITHUB_ACTIONS", None),
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_ACTIONS", "True"),
        ("GITHUB_REPOSITORY", None),
        ("GITHUB_REPOSITORY", "a-fork/policyengine-sim-api"),
        ("GITHUB_REPOSITORY", "policyengine/policyengine-sim-api"),
        ("GITHUB_WORKFLOW_REF", None),
        (
            "GITHUB_WORKFLOW_REF",
            "PolicyEngine/policyengine-sim-api/.github/workflows/ci.yml@refs/heads/main",
        ),
        (
            "GITHUB_WORKFLOW_REF",
            (
                "PolicyEngine/policyengine-sim-api/.github/workflows/"
                "simulation-deploy.reusable.yml@refs/heads/main"
            ),
        ),
        (
            "GITHUB_WORKFLOW_REF",
            (
                "a-fork/policyengine-sim-api/.github/workflows/"
                "simulation-deploy.yml@refs/heads/main"
            ),
        ),
        (
            "GITHUB_WORKFLOW_REF",
            "PolicyEngine/policyengine-sim-api/.github/workflows/simulation-deploy.yml",
        ),
        (
            "GITHUB_WORKFLOW_REF",
            "PolicyEngine/policyengine-sim-api/.github/workflows/simulation-deploy.yml@",
        ),
        (
            "GITHUB_WORKFLOW_REF",
            (
                "PolicyEngine/policyengine-sim-api/.github/workflows/"
                "simulation-deploy.yml@refs/heads/main@extra"
            ),
        ),
        (
            "GITHUB_WORKFLOW_REF",
            (
                "PolicyEngine/policyengine-sim-api/.github/workflows/"
                "simulation-deploy.yml@refs/heads/main extra"
            ),
        ),
        (
            "GITHUB_WORKFLOW_REF",
            (
                "PolicyEngine/policyengine-sim-api/.github/workflows/"
                "simulation-deploy.yml.extra@refs/heads/main"
            ),
        ),
    ],
)
@pytest.mark.usefixtures("serialized_workflow")
def test_routing_guard_requires_every_condition(monkeypatch, variable, value):
    if value is None:
        monkeypatch.delenv(variable, raising=False)
    else:
        monkeypatch.setenv(variable, value)
    with pytest.raises(RuntimeError, match="Routing writes require GitHub Actions"):
        registry._require_serialized_routing_workflow()


@pytest.mark.parametrize(
    ("writer", "kwargs"),
    [
        ("record_routing_plan", {"plan": {}}),
        ("complete_routing_plan", {"plan": {}}),
        (
            "finalize_routing_publication",
            {"release_environment": "beta", "gates_passed": False},
        ),
        ("apply_routing_plan", {"plan": {}}),
        ("restore_routing_plans", {"plans": [{}]}),
        ("restore_routing_plan", {"plan": {}}),
        ("restore_routing_state", {"policyengine_version": "6.2.6"}),
        (
            "publish_routing_state",
            {
                "app_name": "policyengine-simulation-py6-3-0",
                "policyengine_version": "6.3.0",
                "us_version": "1.1.0",
                "uk_version": "2.2.0",
            },
        ),
        ("seed_active_routing_state_from_legacy", {}),
    ],
    ids=[
        "record",
        "complete",
        "finalize",
        "apply",
        "restore-plans",
        "restore-plan",
        "rollback",
        "publish",
        "seed",
    ],
)
@pytest.mark.parametrize(
    ("variable", "value"),
    [
        (None, None),
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_REPOSITORY", "a-fork/policyengine-sim-api"),
        (
            "GITHUB_WORKFLOW_REF",
            "PolicyEngine/policyengine-sim-api/.github/workflows/ci.yml@refs/heads/main",
        ),
    ],
    ids=["local", "not-actions", "fork", "unserialized-workflow"],
)
@pytest.mark.usefixtures("serialized_workflow")
def test_library_writers_refuse_unserialized_calls_before_validation_or_modal_access(
    monkeypatch, patched_modal, writer, kwargs, variable, value
):
    if variable is None:
        for name in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF"):
            monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(variable, value)

    def forbidden_access(*_args, **_kwargs):
        pytest.fail("a refused library write accessed Modal or delegated its write")

    monkeypatch.setattr(registry.modal.Dict, "from_name", forbidden_access)
    if writer == "restore_routing_plan":
        # Each public writer checks its caller, including the one-plan wrapper.
        monkeypatch.setattr(registry, "restore_routing_plans", forbidden_access)
    with pytest.raises(RuntimeError, match="Routing writes require GitHub Actions"):
        getattr(registry, writer)(environment="staging", **kwargs)
    assert patched_modal == {}


@pytest.mark.parametrize(
    "args",
    [
        (
            "--app-name",
            "policyengine-simulation-py6-3-0",
            "--policyengine-version",
            "6.3.0",
            "--us-version",
            "1.1.0",
            "--uk-version",
            "2.2.0",
        ),
        ("--record-plan", "missing-plan.json"),
        ("--complete-plan", "missing-plan.json"),
        (
            "--finalize-publication",
            "--release-environment",
            "beta",
            "--gates-passed",
            "false",
        ),
        ("--apply-plan", "missing-plan.json"),
        ("--restore-plan", "missing-plan.json"),
        ("--restore-latest", "6.2.6"),
        ("--seed-active-from-legacy",),
        ("--apply-plan", "missing-plan.json", "--plan-out", "unused-plan.json"),
        ("--restore-latest", "6.2.6", "--dry-run"),
    ],
    ids=[
        "publish",
        "record",
        "complete",
        "finalize",
        "apply",
        "restore-plan",
        "rollback-version",
        "seed",
        "apply-with-plan-out",
        "restore-with-dry-run",
    ],
)
@pytest.mark.parametrize(
    ("variable", "value"),
    [
        (None, None),
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_REPOSITORY", "a-fork/policyengine-sim-api"),
        (
            "GITHUB_WORKFLOW_REF",
            "PolicyEngine/policyengine-sim-api/.github/workflows/ci.yml@refs/heads/main",
        ),
    ],
    ids=["local", "not-actions", "fork", "unserialized-workflow"],
)
@pytest.mark.usefixtures("serialized_workflow")
def test_main_refuses_unserialized_writes_before_accessing_modal_or_plan_files(
    monkeypatch, patched_modal, capsys, args, variable, value
):
    if variable is None:
        for name in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF"):
            monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(variable, value)

    def forbidden_modal_access(*_args, **_kwargs):
        pytest.fail("a refused CLI write accessed Modal")

    monkeypatch.setattr(registry.modal.Dict, "from_name", forbidden_modal_access)
    with pytest.raises(SystemExit) as refusal:
        _main(monkeypatch, *args)
    assert refusal.value.code == 2
    error = capsys.readouterr().err
    assert "simulation-deploy-main" in error
    for workflow in (
        "simulation-deploy.yml",
        "stage12-deploy.yml",
        "simulation-rollback.yml",
    ):
        assert workflow in error
    assert patched_modal == {}


@pytest.mark.parametrize(
    "mode", ["--print-latest", "--print-identity", "--plan-out", "--dry-run"]
)
def test_main_keeps_read_only_and_plan_modes_available_locally(
    monkeypatch, patched_modal, capsys, tmp_path, mode
):
    for variable in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF"):
        monkeypatch.delenv(variable, raising=False)
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    store = _store(patched_modal, state)
    args = [mode]
    if mode in {"--plan-out", "--dry-run"}:
        if mode == "--plan-out":
            args.append(str(tmp_path / "local-plan.json"))
        args.extend(
            [
                "--app-name",
                _app("6.3.0"),
                "--policyengine-version",
                "6.3.0",
                "--us-version",
                "1.1.0",
                "--uk-version",
                "2.2.0",
            ]
        )
        _manifests(monkeypatch, {"6.3.0": ("1.1.0", "2.2.0")})
    _main(monkeypatch, *args)
    assert capsys.readouterr().out
    assert store.snapshot() == {"active": json.loads(json.dumps(state))}
    if mode == "--plan-out":
        plan = json.loads((tmp_path / "local-plan.json").read_text())
        assert plan["before"] == state
        assert plan["after"]["latest"]["policyengine"] == "6.3.0"


def test_cli_help_explains_serialized_writes_and_has_no_lease_option(
    monkeypatch, capsys
):
    with pytest.raises(SystemExit) as help_exit:
        _main(monkeypatch, "--help")
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "simulation-deploy-main" in help_text
    assert "--lock-wait-seconds" not in help_text
    for workflow in (
        "simulation-deploy.yml",
        "stage12-deploy.yml",
        "simulation-rollback.yml",
    ):
        assert workflow in help_text


def test_applying_a_plan_verifies_the_written_state(monkeypatch, patched_modal):
    state = _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    plan = _plan(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")

    class DroppedWrite(FakeDict):
        def __setitem__(self, key, value):
            pass

    store = DroppedWrite({"active": state})
    patched_modal["staging/simulation-api-routing-state"] = store
    with pytest.raises(
        registry.RoutingConflictError,
        match="published routing state reads back differently",
    ):
        registry.apply_routing_plan(environment="staging", plan=plan)
    assert store.get("active") == state


@pytest.mark.parametrize("first_publication", [False, True])
def test_restoring_a_plan_verifies_the_written_or_removed_state(
    monkeypatch, patched_modal, first_publication
):
    state = (
        None
        if first_publication
        else _publish(monkeypatch, None, "6.2.6", "1.1.0", "2.1.0")
    )
    plan = _plan(monkeypatch, state, "6.3.0", "1.1.0", "2.2.0")

    class DroppedRestore(FakeDict):
        def __setitem__(self, key, value):
            pass

        def pop(self, key):
            return self._data[key]

    store = DroppedRestore({"active": plan["after"]})
    patched_modal["staging/simulation-api-routing-state"] = store
    with pytest.raises(
        registry.RoutingConflictError,
        match="restored routing state reads back differently",
    ):
        registry.restore_routing_plan(environment="staging", plan=plan)
    assert store.get("active") == plan["after"]


def test_runner_loss_cannot_be_adopted_by_the_next_release(monkeypatch, patched_modal):
    """Failed A cannot become successful B's starting state or rollback target."""
    original = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = _store(patched_modal, original)
    failed_a = _plan(monkeypatch, original, "6.1.0", "1.1.0", "2.2.0")
    registry.record_routing_plan(environment="staging", plan=failed_a)
    registry.apply_routing_plan(environment="staging", plan=failed_a)
    # Runner disappears here, before outputs or a release completion record.
    attempted_b = _plan(monkeypatch, store.get("active"), "6.2.0", "1.2.0", "2.3.0")
    for operation in (registry.record_routing_plan, registry.apply_routing_plan):
        with pytest.raises(
            registry.RoutingConflictError, match="Unfinished routing publication"
        ):
            operation(environment="staging", plan=attempted_b)
    assert store.get("active") == failed_a["after"]
    assert store.get("publication") == {"status": "pending", "plan": failed_a}

    # Explicit recovery withdraws A before a later release can publish.
    assert (
        registry.restore_routing_plan(environment="staging", plan=failed_a)
        == "restored"
    )
    assert store.get("active") == original
    assert store.get("publication") == {"status": "restored", "plan": failed_a}
    healthy_b = _plan(monkeypatch, original, "6.2.0", "1.2.0", "2.3.0")
    registry.record_routing_plan(environment="staging", plan=healthy_b)
    registry.apply_routing_plan(environment="staging", plan=healthy_b)
    registry.complete_routing_plan(environment="staging", plan=healthy_b)
    assert "6.1.0" not in store.get("active")["routes"]["policyengine"]
    with pytest.raises(registry.RoutingConflictError, match="nothing was restored"):
        registry.restore_routing_plan(environment="staging", plan=failed_a)
    # A subsequent failed release restores healthy B and cannot resurrect A.
    failed_c = _plan(monkeypatch, healthy_b["after"], "6.3.0", "1.3.0", "2.4.0")
    registry.record_routing_plan(environment="staging", plan=failed_c)
    registry.apply_routing_plan(environment="staging", plan=failed_c)
    assert (
        registry.restore_routing_plan(environment="staging", plan=failed_c)
        == "restored"
    )
    assert store.get("active") == healthy_b["after"]
    assert "6.1.0" not in store.get("active")["routes"]["policyengine"]


def test_a_failed_same_version_redeploy_is_resolved_by_its_restore(
    monkeypatch, patched_modal
):
    """A redeploy that fails a gate must not block later releases.

    A plan's published state carries its own publication id, so even a
    same-version redeploy differs from what it replaces and has a restore.
    """
    live = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = _store(patched_modal, live)
    redeploy = _plan(monkeypatch, live, "6.0.0", "1.1.0", "2.1.0")
    assert redeploy["before_sha256"] != redeploy["after_sha256"]
    changed = {
        key
        for key in redeploy["after"]
        if redeploy["after"][key] != redeploy["before"][key]
    }
    assert changed == {"generation"}
    registry.record_routing_plan(environment="staging", plan=redeploy)
    registry.apply_routing_plan(environment="staging", plan=redeploy)
    # A release gate fails here. Until the restore runs, nothing else may write.
    later = _plan(monkeypatch, live, "6.1.0", "1.1.0", "2.2.0")
    with pytest.raises(
        registry.RoutingConflictError, match="Unfinished routing publication"
    ):
        registry.record_routing_plan(environment="staging", plan=later)
    assert (
        registry.restore_routing_plan(environment="staging", plan=redeploy)
        == "restored"
    )
    assert store.get("active") == live
    assert store.get("publication") == {"status": "restored", "plan": redeploy}
    registry.record_routing_plan(environment="staging", plan=later)


@pytest.mark.parametrize("first_publication", [False, True])
def test_recorded_but_unpublished_plan_blocks_writes_until_restored(
    monkeypatch, patched_modal, first_publication
):
    before = (
        None
        if first_publication
        else _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    )
    store = _store(patched_modal, before)
    pending = _plan(monkeypatch, before, "6.1.0", "1.1.0", "2.2.0")
    registry.record_routing_plan(environment="staging", plan=pending)
    # Covers loss after recording, before upload or before apply.
    with pytest.raises(
        registry.RoutingConflictError, match="Unfinished routing publication"
    ):
        registry.record_routing_plan(environment="staging", plan=pending)
    assert (
        registry.restore_routing_plan(environment="staging", plan=pending)
        == "unchanged"
    )
    assert store.get("active") == before
    assert store.get("publication")["status"] == "restored"
    registry.record_routing_plan(environment="staging", plan=pending)


@pytest.mark.parametrize("writer", ["publish", "latest", "seed", "restore"])
def test_every_other_routing_write_refuses_an_unfinished_release(
    monkeypatch, patched_modal, writer
):
    before = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = _store(patched_modal, before)
    pending = _plan(monkeypatch, before, "6.1.0", "1.1.0", "2.2.0")
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    saved = json.loads(json.dumps(store.snapshot()))
    other = _plan(monkeypatch, pending["after"], "6.2.0", "1.2.0", "2.3.0")

    def write_routing():
        if writer == "publish":
            registry.publish_routing_state(
                environment="staging",
                app_name=_app("6.2.0"),
                policyengine_version="6.2.0",
                us_version="1.2.0",
                uk_version="2.3.0",
            )
        elif writer == "latest":
            registry.restore_routing_state(
                environment="staging", policyengine_version="6.0.0"
            )
        elif writer == "seed":
            registry.seed_active_routing_state_from_legacy(environment="staging")
        else:
            registry.restore_routing_plans(environment="staging", plans=[other])

    with pytest.raises(
        registry.RoutingConflictError, match="Unfinished routing publication"
    ):
        write_routing()
    assert store.snapshot() == saved


def test_only_matching_live_publication_can_be_completed(monkeypatch, patched_modal):
    before = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = _store(patched_modal, before)
    pending = _plan(monkeypatch, before, "6.1.0", "1.1.0", "2.2.0")
    with pytest.raises(registry.RoutingConflictError, match="no matching unfinished"):
        registry.complete_routing_plan(environment="staging", plan=pending)
    registry.record_routing_plan(environment="staging", plan=pending)
    with pytest.raises(
        registry.RoutingConflictError, match="routing state is not live"
    ):
        registry.complete_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    other = _plan(monkeypatch, before, "6.1.0", "1.1.0", "2.2.0")
    with pytest.raises(registry.RoutingConflictError, match="no matching unfinished"):
        registry.complete_routing_plan(environment="staging", plan=other)
    assert store.get("publication")["status"] == "pending"
    registry.complete_routing_plan(environment="staging", plan=pending)
    assert store.get("publication") == {"status": "completed", "plan": pending}


@pytest.mark.parametrize("operation", ["apply", "restore", "complete"])
def test_publication_ownership_compares_the_entire_plan(
    monkeypatch, patched_modal, operation
):
    """A reused publication id never grants permission to a changed plan."""
    before = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = _store(patched_modal, before)
    pending = _plan(monkeypatch, before, "6.1.0", "1.1.0", "2.2.0")
    registry.record_routing_plan(environment="staging", plan=pending)
    if operation != "apply":
        registry.apply_routing_plan(environment="staging", plan=pending)
    changed = json.loads(json.dumps(pending))
    changed["advances_latest"] = not pending["advances_latest"]
    registry.validate_routing_plan(changed)
    assert changed["publication_id"] == pending["publication_id"]
    saved = json.loads(json.dumps(store.snapshot()))
    writer = {
        "apply": registry.apply_routing_plan,
        "restore": registry.restore_routing_plan,
        "complete": registry.complete_routing_plan,
    }[operation]
    with pytest.raises(registry.RoutingConflictError):
        writer(environment="staging", plan=changed)
    assert store.snapshot() == saved


@pytest.mark.parametrize(
    ("scenario", "applied", "gates_passed", "expected"),
    [
        ("failed_release", True, False, "restored"),
        ("successful_release", True, True, "completed"),
        ("upload_failure_before_apply", False, False, "restored"),
        ("failed_jobs_rerun", True, True, "completed"),
        ("failed_jobs_rerun_gate_failure", True, False, "restored"),
        ("failure_inside_deployment_ready", True, True, "completed"),
        ("passing_gates_but_apply_never_ran", False, True, "restored"),
    ],
)
@pytest.mark.parametrize("first_publication", [False, True])
def test_finalization_finishes_each_release_and_allows_the_next_one(
    monkeypatch,
    patched_modal,
    scenario,
    applied,
    gates_passed,
    expected,
    first_publication,
):
    """Upload loss and attempt 2 use the full journal, with no artifact reads."""
    before = (
        None
        if first_publication
        else _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    )
    store = _store(patched_modal, before)
    pending = _plan(
        monkeypatch,
        before,
        "6.1.0",
        "1.1.0",
        "2.2.0",
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    if applied:
        registry.apply_routing_plan(environment="staging", plan=pending)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2" if "rerun" in scenario else "1")
    assert (
        registry.finalize_routing_publication(
            environment="staging",
            release_environment="beta",
            publication_run="74401",
            gates_passed=gates_passed,
        )
        == expected
    )
    expected_live = pending["after"] if expected == "completed" else before
    assert store.get("active") == expected_live
    assert store.get("publication") == {"status": expected, "plan": pending}
    # deployment_ready is a reporter after finalization. Losing that reporter
    # cannot strand the journal or block the next run.
    if scenario == "failure_inside_deployment_ready":
        assert store.get("publication")["status"] == "completed"
    later = _plan(
        monkeypatch,
        expected_live,
        "6.2.0",
        "1.2.0",
        "2.3.0",
        publication_id="74402-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=later)
    registry.apply_routing_plan(environment="staging", plan=later)
    assert store.get("active") == later["after"]


def test_lost_finalizer_is_recovered_by_operator_without_an_artifact(
    monkeypatch, patched_modal
):
    before = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = _store(patched_modal, before)
    pending = _plan(
        monkeypatch,
        before,
        "6.1.0",
        "1.1.0",
        "2.2.0",
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    later = _plan(monkeypatch, pending["after"], "6.2.0", "1.2.0", "2.3.0")
    with pytest.raises(
        registry.RoutingConflictError, match="Unfinished routing publication"
    ):
        registry.record_routing_plan(environment="staging", plan=later)
    assert store.get("publication") == {"status": "pending", "plan": pending}
    assert (
        registry.finalize_routing_publication(
            environment="staging", release_environment="beta", gates_passed=False
        )
        == "restored"
    )
    assert store.get("active") == before
    assert store.get("publication") == {"status": "restored", "plan": pending}
    recovered_later = _plan(monkeypatch, before, "6.2.0", "1.2.0", "2.3.0")
    registry.record_routing_plan(environment="staging", plan=recovered_later)
    registry.apply_routing_plan(environment="staging", plan=recovered_later)
    assert store.get("active") == recovered_later["after"]


@pytest.mark.parametrize("gates_passed", [False, True])
@pytest.mark.parametrize(
    "publication_id",
    [
        "74400-1-beta",
        "744010-1-beta",
        "74401-1-prod",
        "74401-1-beta-other",
        "74401-invalid-beta",
        "invalid-1-beta",
    ],
)
def test_finalizer_leaves_a_foreign_pending_publication_untouched(
    monkeypatch, patched_modal, gates_passed, publication_id
):
    store = _store(patched_modal, None)
    pending = _plan(
        monkeypatch,
        None,
        "6.1.0",
        "1.1.0",
        "2.2.0",
        publication_id=publication_id,
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    saved = json.loads(json.dumps(store.snapshot()))
    assert (
        registry.finalize_routing_publication(
            environment="staging",
            release_environment="beta",
            publication_run="74401",
            gates_passed=gates_passed,
        )
        == "foreign-publication"
    )
    assert store.snapshot() == saved


@pytest.mark.parametrize(
    ("publication_run", "gates_passed"),
    [("74401", False), ("74401", True), (None, False)],
)
def test_finalizer_conflict_is_loud_and_keeps_the_journal_pending(
    monkeypatch, patched_modal, publication_run, gates_passed
):
    before = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    store = _store(patched_modal, before)
    pending = _plan(
        monkeypatch,
        before,
        "6.1.0",
        "1.1.0",
        "2.2.0",
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    store["active"] = _publish(monkeypatch, pending["after"], "6.2.0", "1.2.0", "2.3.0")
    saved = json.loads(json.dumps(store.snapshot()))
    with pytest.raises(registry.RoutingConflictError, match="nothing was restored"):
        registry.finalize_routing_publication(
            environment="staging",
            release_environment="beta",
            publication_run=publication_run,
            gates_passed=gates_passed,
        )
    assert store.snapshot() == saved


@pytest.mark.parametrize("gates_passed", [False, True])
@pytest.mark.parametrize("journal", ["missing_store", "absent", "foreign_completed"])
def test_finalizer_without_its_pending_record_changes_nothing(
    monkeypatch, patched_modal, gates_passed, journal
):
    if journal != "missing_store":
        before = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
        store = _store(patched_modal, before)
        if journal == "foreign_completed":
            plan = _plan(
                monkeypatch,
                before,
                "6.1.0",
                "1.1.0",
                "2.2.0",
                publication_id="74400-1-beta",
            )
            registry.record_routing_plan(environment="staging", plan=plan)
            registry.apply_routing_plan(environment="staging", plan=plan)
            registry.complete_routing_plan(environment="staging", plan=plan)
        saved = json.loads(json.dumps(store.snapshot()))
    assert (
        registry.finalize_routing_publication(
            environment="staging",
            release_environment="beta",
            publication_run="74401",
            gates_passed=gates_passed,
        )
        == "no-pending"
    )
    if journal == "missing_store":
        assert patched_modal == {}
    else:
        assert store.snapshot() == saved


@pytest.mark.parametrize("gates_passed", [False, True])
def test_retry_after_finalization_returns_the_prior_outcome_without_writing(
    monkeypatch, patched_modal, gates_passed
):
    store = _store(patched_modal, None)
    pending = _plan(
        monkeypatch,
        None,
        "6.1.0",
        "1.1.0",
        "2.2.0",
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    args = {
        "environment": "staging",
        "release_environment": "beta",
        "publication_run": "74401",
        "gates_passed": gates_passed,
    }
    expected = "completed" if gates_passed else "restored"
    assert registry.finalize_routing_publication(**args) == expected
    saved = json.loads(json.dumps(store.snapshot()))

    def forbidden_write(*_args, **_kwargs):
        pytest.fail("an already finalized publication was written again")

    monkeypatch.setattr(registry, "_write_publication_record", forbidden_write)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    assert registry.finalize_routing_publication(**args) == expected
    assert store.snapshot() == saved


def test_operator_cannot_complete_pending_publications(patched_modal):
    store = _store(patched_modal, None)
    with pytest.raises(
        ValueError, match="operator recovery requires gates_passed=false"
    ):
        registry.finalize_routing_publication(
            environment="staging", release_environment="beta", gates_passed=True
        )
    assert store.snapshot() == {}


def test_completed_finalizer_retry_cannot_report_a_release_that_is_no_longer_live(
    monkeypatch, patched_modal
):
    pending = _plan(
        monkeypatch,
        None,
        "6.1.0",
        "1.1.0",
        "2.2.0",
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    registry.complete_routing_plan(environment="staging", plan=pending)
    store = patched_modal["staging/simulation-api-routing-state"]
    store["active"] = _publish(monkeypatch, pending["after"], "6.2.0", "1.2.0", "2.3.0")
    saved = json.loads(json.dumps(store.snapshot()))
    assert (
        registry.finalize_routing_publication(
            environment="staging",
            release_environment="beta",
            publication_run="74401",
            gates_passed=True,
        )
        == "no-pending"
    )
    assert store.snapshot() == saved


def test_finalizer_owns_prod_publications_in_the_main_modal_environment(
    monkeypatch, patched_modal
):
    pending = _plan(
        monkeypatch,
        None,
        "6.1.0",
        "1.1.0",
        "2.2.0",
        publication_id="74401-1-prod",
    )
    registry.record_routing_plan(environment="main", plan=pending)
    registry.apply_routing_plan(environment="main", plan=pending)
    assert (
        registry.finalize_routing_publication(
            environment="main",
            release_environment="prod",
            publication_run="74401",
            gates_passed=True,
        )
        == "completed"
    )
    store = patched_modal["main/simulation-api-routing-state"]
    assert store.get("publication") == {"status": "completed", "plan": pending}


def test_finalizer_rejects_invalid_release_environments(patched_modal):
    with pytest.raises(ValueError, match="release_environment must be beta or prod"):
        registry.finalize_routing_publication(
            environment="staging", release_environment="staging", gates_passed=False
        )
    assert patched_modal == {}


@pytest.mark.parametrize(
    "publication_id",
    [
        "invalid-1-beta",
        "74401-invalid-beta",
        "74401-1-prod",
        "74401-1-beta-extra",
        "b88e8c1b01384bc88885657dcf1b8254",
    ],
)
def test_operator_restores_any_valid_pending_plan_in_the_selected_store(
    monkeypatch, patched_modal, publication_id
):
    pending = _plan(
        monkeypatch,
        None,
        "6.1.0",
        "1.1.0",
        "2.2.0",
        publication_id=publication_id,
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    store = patched_modal["staging/simulation-api-routing-state"]
    saved = json.loads(json.dumps(store.snapshot()))
    assert (
        registry.finalize_routing_publication(
            environment="staging", release_environment="beta", gates_passed=False
        )
        == "restored"
    )
    assert saved["publication"]["status"] == "pending"
    assert store.get("active") is None
    assert store.get("publication") == {"status": "restored", "plan": pending}


@pytest.mark.parametrize("status", ["pending", "completed", "restored"])
def test_publication_records_are_read_back(monkeypatch, patched_modal, status):
    before = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    plan = _plan(monkeypatch, before, "6.1.0", "1.1.0", "2.2.0")

    class DroppedJournalWrite(FakeDict):
        def __setitem__(self, key, value):
            if key != "publication":
                super().__setitem__(key, value)

    initial = {"active": before if status == "pending" else plan["after"]}
    if status != "pending":
        initial["publication"] = {"status": "pending", "plan": plan}
    store = DroppedJournalWrite(initial)
    patched_modal["staging/simulation-api-routing-state"] = store
    operation = {
        "pending": registry.record_routing_plan,
        "completed": registry.complete_routing_plan,
        "restored": registry.restore_routing_plan,
    }[status]
    with pytest.raises(
        registry.RoutingConflictError, match="publication record reads back differently"
    ):
        operation(environment="staging", plan=plan)
    assert store.get("active") == (plan["after"] if status == "completed" else before)
    assert store.get("publication") == initial.get("publication")


def test_recording_checks_the_live_starting_state(monkeypatch, patched_modal):
    before = _publish(monkeypatch, None, "6.0.0", "1.1.0", "2.1.0")
    plan = _plan(monkeypatch, before, "6.1.0", "1.1.0", "2.2.0")
    later = _publish(monkeypatch, before, "6.2.0", "1.2.0", "2.3.0")
    store = _store(patched_modal, later)
    with pytest.raises(registry.RoutingConflictError, match="changed before recording"):
        registry.record_routing_plan(environment="staging", plan=plan)
    assert store.snapshot() == {"active": later}


@pytest.mark.parametrize(
    "record", ["garbled", {"status": "unknown"}, {"status": "completed", "plan": {}}]
)
def test_malformed_publication_record_refuses_writes(
    monkeypatch, patched_modal, record
):
    store = _store(patched_modal, None)
    store["publication"] = record
    plan = _plan(monkeypatch, None, "6.1.0", "1.1.0", "2.2.0")
    with pytest.raises((registry.RoutingConflictError, ValueError)):
        registry.apply_routing_plan(environment="staging", plan=plan)
    assert store.snapshot() == {"publication": record}


def test_cli_records_and_completes_release_publications(
    monkeypatch, patched_modal, tmp_path, capsys
):
    plan = _plan(monkeypatch, None, "6.1.0", "1.1.0", "2.2.0")
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    _main(monkeypatch, "--record-plan", str(path))
    store = patched_modal["staging/simulation-api-routing-state"]
    assert store.get("active") is None
    assert store.get("publication") == {"status": "pending", "plan": plan}
    _main(monkeypatch, "--apply-plan", str(path))
    _main(monkeypatch, "--complete-plan", str(path))
    assert store.get("active") == plan["after"]
    assert store.get("publication") == {"status": "completed", "plan": plan}
    assert ": pending" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("publication_run", "gates_passed", "expected"),
    [
        ("74401", "true", "completed"),
        ("74401", "false", "restored"),
        ("74400", "false", "foreign-publication"),
        (None, "false", "restored"),
    ],
)
def test_cli_finalizes_the_printed_journal_and_reports_its_outcome(
    monkeypatch,
    patched_modal,
    tmp_path,
    capsys,
    publication_run,
    gates_passed,
    expected,
):
    store = _store(patched_modal, None)
    pending = _plan(
        monkeypatch,
        None,
        "6.1.0",
        "1.1.0",
        "2.2.0",
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    output = tmp_path / "github-output"
    output.write_text("existing=kept\n")
    args = [
        "--finalize-publication",
        "--release-environment",
        "beta",
        "--gates-passed",
        gates_passed,
        "--github-output",
        str(output),
    ]
    if publication_run is not None:
        args.extend(["--publication-run", publication_run])
    # Recovery must display the full pending record before the first write.
    original = registry.finalize_routing_publication

    def finalizer(**kwargs):
        printed = capsys.readouterr().out
        assert printed.startswith("Pending routing publication:\n")
        assert json.loads(printed.partition("\n")[2]) == {
            "status": "pending",
            "plan": pending,
        }
        return original(**kwargs)

    monkeypatch.setattr(registry, "finalize_routing_publication", finalizer)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    _main(monkeypatch, *args)
    assert output.read_text() == f"existing=kept\noutcome={expected}\n"
    printed = capsys.readouterr().out
    assert f"Routing publication finalization: {expected}" in printed
    if expected == "foreign-publication":
        assert "belongs to another run or environment" in printed
        assert store.get("publication") == {"status": "pending", "plan": pending}
        assert store.get("active") == pending["after"]
    else:
        assert store.get("publication") == {"status": expected, "plan": pending}
        assert store.get("active") == (
            pending["after"] if expected == "completed" else None
        )


def test_cli_missing_journal_is_explicit_and_reports_no_pending(
    monkeypatch, patched_modal, tmp_path, capsys
):
    output = tmp_path / "github-output"
    _main(
        monkeypatch,
        "--finalize-publication",
        "--publication-run",
        "74401",
        "--release-environment",
        "beta",
        "--gates-passed",
        "true",
        "--github-output",
        str(output),
    )
    assert "No pending routing publication" in capsys.readouterr().out
    assert output.read_text() == "outcome=no-pending\n"
    assert patched_modal == {}


def test_cli_output_loss_is_retryable_after_the_journal_is_completed(
    monkeypatch, patched_modal, tmp_path, capsys
):
    pending = _plan(
        monkeypatch,
        None,
        "6.1.0",
        "1.1.0",
        "2.2.0",
        publication_id="74401-1-beta",
    )
    registry.record_routing_plan(environment="staging", plan=pending)
    registry.apply_routing_plan(environment="staging", plan=pending)
    args = [
        "--finalize-publication",
        "--publication-run",
        "74401",
        "--release-environment",
        "beta",
        "--gates-passed",
        "true",
        "--github-output",
    ]
    with pytest.raises(FileNotFoundError):
        _main(monkeypatch, *args, str(tmp_path / "missing" / "output"))
    store = patched_modal["staging/simulation-api-routing-state"]
    assert store.get("publication") == {"status": "completed", "plan": pending}
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    _main(monkeypatch, *args, str(output))
    assert "No pending routing publication" in capsys.readouterr().out
    assert output.read_text() == "outcome=completed\n"
    assert store.get("publication") == {"status": "completed", "plan": pending}


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--release-environment", "beta"),
        ("--gates-passed", "false"),
        ("--release-environment", "beta", "--gates-passed", "true"),
        ("--release-environment", "beta", "--gates-passed", "perhaps"),
    ],
)
def test_cli_finalizer_requires_explicit_safe_inputs(monkeypatch, patched_modal, args):
    with pytest.raises(SystemExit) as exc:
        _main(monkeypatch, "--finalize-publication", *args)
    assert exc.value.code == 2
    assert patched_modal == {}


def test_pending_plan_can_be_inspected_locally_before_upload(
    monkeypatch, patched_modal, capsys
):
    plan = _plan(monkeypatch, None, "6.1.0", "1.1.0", "2.2.0")
    registry.record_routing_plan(environment="staging", plan=plan)
    store = patched_modal["staging/simulation-api-routing-state"]
    saved = json.loads(json.dumps(store.snapshot()))
    for variable in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_WORKFLOW_REF"):
        monkeypatch.delenv(variable, raising=False)
    _main(monkeypatch, "--print-pending-plan")
    assert json.loads(capsys.readouterr().out) == plan
    assert store.snapshot() == saved


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
