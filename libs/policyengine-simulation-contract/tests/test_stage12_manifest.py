"""Tests for the separate Stage 12 v2 worker manifest."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from policyengine_simulation_contract.stage12_bundle import (
    ResolvedStage12Bundle,
    Stage12BundleManifest,
    Stage12CountryBundle,
    Stage12Dataset,
)
from policyengine_simulation_contract.stage12_manifest import (
    ACTIVE_MANIFEST_KEY,
    V1_ROUTING_STATE_NAME,
    V2_VERSION_MANIFEST_NAME,
    V2CountryWorker,
    V2ManifestLoader,
    V2WorkerValidation,
    V2WorkerVersion,
    assert_separate_manifest_names,
    manifest_digest,
    publish_v2_manifest,
    v2_application_name,
    validate_manifest_has_no_credentials,
)


class FakeStore:
    def __init__(self, initial: dict | None = None):
        self.values = dict(initial or {})
        self.writes: list[tuple[str, object]] = []

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def __setitem__(self, key: str, value: object) -> None:
        self.writes.append((key, value))
        self.values[key] = value


def _resolved_bundle(version: str = "5.2.0") -> ResolvedStage12Bundle:
    countries = []
    for country, package, package_version, dataset in (
        ("us", "policyengine-us", "1.764.6", "populace_us_2024"),
        ("uk", "policyengine-uk", "2.90.2", "populace_uk_2023"),
    ):
        revision = f"{dataset}-revision"
        countries.append(
            Stage12CountryBundle(
                country=country,
                country_package_name=package,
                country_package_version=package_version,
                country_package_requirement=f"{package}=={package_version}",
                data_package_name="populace-data",
                data_package_version="0.1.0",
                data_release_version=revision,
                data_artifact_revision=revision,
                default_dataset=dataset,
                default_dataset_uri=f"hf://policyengine/data/{dataset}.h5@{revision}",
                datasets=(
                    Stage12Dataset(
                        identity=dataset,
                        uri=f"hf://policyengine/data/{dataset}.h5@{revision}",
                        artifact_revision=revision,
                        sha256="a" * 64,
                        repo_type="dataset",
                    ),
                ),
            )
        )
    bundle = Stage12BundleManifest(
        policyengine_version=version,
        policyengine_requirement=f"policyengine=={version}",
        core_package_version="3.30.1",
        core_package_requirement="policyengine-core==3.30.1",
        countries=tuple(countries),
    )
    return ResolvedStage12Bundle(
        bundle=bundle,
        bundle_manifest_sha256="b" * 64,
    )


def _worker(version: str = "5.2.0") -> V2WorkerVersion:
    resolved = _resolved_bundle(version)
    application_name = v2_application_name(version)
    return V2WorkerVersion(
        application_name=application_name,
        report_coordinator_callable="coordinate_report",
        countries=(
            V2CountryWorker(
                country="us",
                single_simulation_callable="run_single_simulation_us",
            ),
            V2CountryWorker(
                country="uk",
                single_simulation_callable="run_single_simulation_uk",
            ),
        ),
        bundle=resolved.bundle,
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        validation=V2WorkerValidation(
            validated=True,
            application_name=application_name,
            bundle_manifest_sha256=resolved.bundle_manifest_sha256,
            validated_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
            validation_invocation_id="validation-123",
            country_validation_invocation_ids={
                "us": "validation-us-123",
                "uk": "validation-uk-123",
            },
        ),
    )


def test_v2_application_name_cannot_collide_with_v1() -> None:
    assert v2_application_name("5.2.0") == "policyengine-simulation-v2-py5-2-0"
    assert not v2_application_name("5.2.0").startswith("policyengine-simulation-py")


def test_publish_writes_one_complete_v2_document() -> None:
    store = FakeStore()

    manifest = publish_v2_manifest(store=store, worker=_worker())

    assert store.writes == [(ACTIVE_MANIFEST_KEY, manifest.model_dump(mode="json"))]
    assert manifest.default_version == "5.2.0"
    assert manifest.versions["5.2.0"].validation.validated is True


def test_loader_retains_its_last_valid_v2_document() -> None:
    store = FakeStore()
    expected = publish_v2_manifest(store=store, worker=_worker())
    loader = V2ManifestLoader(store)
    assert loader.load() == expected

    store.values[ACTIVE_MANIFEST_KEY] = {"schema_version": 999}

    assert loader.load() == expected


def test_loader_never_falls_back_to_v1_state() -> None:
    v1_store = FakeStore({ACTIVE_MANIFEST_KEY: {"schema_version": 1}})
    v2_store = FakeStore()
    loader = V2ManifestLoader(v2_store)

    with pytest.raises(ValueError, match="no valid v2"):
        loader.load()

    assert v1_store.values == {ACTIVE_MANIFEST_KEY: {"schema_version": 1}}


def test_v1_and_v2_publication_and_removal_are_bidirectionally_isolated() -> None:
    v1_store = FakeStore({ACTIVE_MANIFEST_KEY: {"v1": "unchanged"}})
    v2_store = FakeStore()
    v1_before = dict(v1_store.values)

    manifest = publish_v2_manifest(store=v2_store, worker=_worker())
    v2_before = dict(v2_store.values)
    assert v1_store.values == v1_before

    v1_store[ACTIVE_MANIFEST_KEY] = {"v1": "new"}
    assert v2_store.values == v2_before
    assert manifest_digest(V2ManifestLoader(v2_store).load()) == manifest_digest(
        manifest
    )

    v2_store.values.pop(ACTIVE_MANIFEST_KEY)
    assert v1_store.values == {ACTIVE_MANIFEST_KEY: {"v1": "new"}}


def test_manifest_rejects_incomplete_or_unvalidated_worker() -> None:
    value = _worker().model_dump(mode="json")
    value.pop("report_coordinator_callable")
    with pytest.raises(ValueError):
        V2WorkerVersion.model_validate(value)

    value = _worker().model_dump(mode="json")
    value["validation"]["validated"] = False
    with pytest.raises(ValueError):
        V2WorkerVersion.model_validate(value)


def test_manifest_rejects_credential_like_fields() -> None:
    with pytest.raises(ValueError, match="credential-like"):
        validate_manifest_has_no_credentials(
            {"versions": {"5.2.0": {"access_token": "not-allowed"}}}
        )


def test_storage_names_must_be_distinct() -> None:
    assert_separate_manifest_names(
        v1_name=V1_ROUTING_STATE_NAME,
        v2_name=V2_VERSION_MANIFEST_NAME,
    )
    with pytest.raises(ValueError, match="different"):
        assert_separate_manifest_names(v1_name="same", v2_name="same")
