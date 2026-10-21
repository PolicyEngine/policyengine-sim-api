"""Tests for the separate Stage 12 v2 worker manifest."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from policyengine_simulation_contract.stage12_bundle import (
    ResolvedStage12Bundle,
    Stage12BundleManifest,
    Stage12CountryBundle,
    Stage12Dataset,
    Stage12RegionDataset,
    region_type_of,
)
from policyengine_simulation_contract.stage12_manifest import (
    ACTIVE_MANIFEST_KEY,
    PRE_CACHE_MANIFEST_KEY,
    REGION_DATASETS_MANIFEST_KEY,
    V1_ROUTING_STATE_NAME,
    V2_VERSION_MANIFEST_NAME,
    Stage12CacheRelease,
    V2CountryWorker,
    V2ManifestLoader,
    V2WorkerValidation,
    V2WorkerVersion,
    assert_separate_manifest_names,
    manifest_digest,
    pre_cache_manifest_payload,
    pre_region_datasets_manifest_payload,
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
            validated_at=datetime(2026, 9, 14, tzinfo=UTC),
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


def _cached_worker(version: str = "5.2.0") -> V2WorkerVersion:
    worker = _worker(version)
    cache = Stage12CacheRelease(
        manifest_sha256="c" * 64,
        partition_sha256="d" * 64,
        years=(2026, 2027, 2025),
    )
    return worker.model_copy(
        update={
            "cache": cache,
            "validation": worker.validation.model_copy(update={"cache": cache}),
        }
    )


def test_v2_application_name_allows_exactly_64_characters() -> None:
    version = "5." + "1" * 33

    assert len(v2_application_name(version)) == 64
    with pytest.raises(
        ValueError, match=r"^v2 Modal application name exceeds 64 characters$"
    ):
        v2_application_name(version + "1")


@pytest.mark.parametrize("version", ["", ".5.2.0", "5.2.0 ", "5/2"])
def test_v2_application_name_rejects_versions_a_modal_name_cannot_hold(
    version: str,
) -> None:
    with pytest.raises(
        ValueError,
        match=r"^PolicyEngine\.py version cannot form a Modal application name$",
    ):
        v2_application_name(version)


def _regional_worker(version: str = "5.2.0") -> V2WorkerVersion:
    """Return a cached worker whose US bundle names each region type's dataset."""
    worker = _cached_worker(version)
    countries = tuple(
        country.model_copy(
            update={
                "region_datasets": (
                    Stage12RegionDataset(
                        region_type="national", dataset=country.default_dataset
                    ),
                    Stage12RegionDataset(
                        region_type="state", dataset=f"{country.default_dataset}_local"
                    ),
                )
            }
        )
        if country.country == "us"
        else country
        for country in worker.bundle.countries
    )
    return worker.model_copy(
        update={"bundle": worker.bundle.model_copy(update={"countries": countries})}
    )


def test_publish_writes_one_complete_v2_document_per_key() -> None:
    store = FakeStore()

    manifest = publish_v2_manifest(store=store, worker=_regional_worker())

    assert store.writes == [
        (REGION_DATASETS_MANIFEST_KEY, manifest.model_dump(mode="json")),
        (ACTIVE_MANIFEST_KEY, pre_region_datasets_manifest_payload(manifest)),
        (PRE_CACHE_MANIFEST_KEY, pre_cache_manifest_payload(manifest)),
    ]
    assert manifest.default_version == "5.2.0"
    assert manifest.versions["5.2.0"].validation.validated is True
    assert V2ManifestLoader(store).load().versions["5.2.0"].cache is not None


def test_readers_released_before_the_cache_get_only_fields_they_accept() -> None:
    store = FakeStore()
    publish_v2_manifest(store=store, worker=_cached_worker())

    # The fields of each model before Stage 12 cache releases. Those readers
    # forbid any other field, so adding one here would stop them loading.
    pre_cache = store.values[PRE_CACHE_MANIFEST_KEY]
    assert set(pre_cache) == {
        "default_version",
        "generation",
        "schema_version",
        "versions",
    }
    worker = pre_cache["versions"]["5.2.0"]
    assert set(worker) == {
        "application_name",
        "bundle",
        "bundle_manifest_sha256",
        "countries",
        "report_coordinator_callable",
        "validation",
    }
    assert set(worker["validation"]) == {
        "application_name",
        "bundle_manifest_sha256",
        "country_validation_invocation_ids",
        "validated",
        "validated_at",
        "validation_invocation_id",
    }


# The fields of a country bundle before region datasets. Readers released
# before them forbid any other field, so adding one would stop them loading.
_COUNTRY_BUNDLE_FIELDS_BEFORE_REGION_DATASETS = {
    "country",
    "country_package_name",
    "country_package_version",
    "country_package_requirement",
    "data_package_name",
    "data_package_version",
    "data_release_version",
    "data_artifact_revision",
    "default_dataset",
    "default_dataset_uri",
    "datasets",
}


@pytest.mark.parametrize("key", [ACTIVE_MANIFEST_KEY, PRE_CACHE_MANIFEST_KEY])
def test_readers_released_before_region_datasets_get_only_fields_they_accept(
    key: str,
) -> None:
    store = FakeStore()
    publish_v2_manifest(store=store, worker=_regional_worker())

    worker = store.values[key]["versions"]["5.2.0"]
    for country in worker["bundle"]["countries"]:
        assert set(country) == _COUNTRY_BUNDLE_FIELDS_BEFORE_REGION_DATASETS


def test_the_region_dataset_key_keeps_region_datasets_and_cache() -> None:
    store = FakeStore()
    publish_v2_manifest(store=store, worker=_regional_worker())

    loaded = V2ManifestLoader(store).load().versions["5.2.0"]
    us = next(country for country in loaded.bundle.countries if country.country == "us")
    assert us.dataset_for_region_type("state") == "populace_us_2024_local"
    assert us.dataset_for_region_type("national") == "populace_us_2024"
    assert us.dataset_for_region_type("congressional_district") is None
    assert loaded.cache is not None


def test_a_bundle_published_before_region_datasets_has_none() -> None:
    worker = _cached_worker()
    us = next(country for country in worker.bundle.countries if country.country == "us")

    assert us.region_datasets is None
    assert us.dataset_for_region_type("state") is None


def test_first_publish_with_region_datasets_keeps_versions_from_the_cache_key() -> None:
    earlier = _cached_worker("5.1.0").model_dump(mode="json")
    store = FakeStore(
        {
            ACTIVE_MANIFEST_KEY: {
                "schema_version": 1,
                "generation": "5.1.0:earlier",
                "default_version": "5.1.0",
                "versions": {"5.1.0": earlier},
            }
        }
    )
    assert V2ManifestLoader(store).load().default_version == "5.1.0"

    manifest = publish_v2_manifest(store=store, worker=_regional_worker())

    assert sorted(manifest.versions) == ["5.1.0", "5.2.0"]
    assert V2ManifestLoader(store).load() == manifest


def test_first_publish_with_cache_keeps_versions_from_the_pre_cache_key() -> None:
    earlier = _worker("5.1.0").model_dump(
        mode="json", exclude={"cache": True, "validation": {"cache"}}
    )
    store = FakeStore(
        {
            PRE_CACHE_MANIFEST_KEY: {
                "schema_version": 1,
                "generation": "5.1.0:earlier",
                "default_version": "5.1.0",
                "versions": {"5.1.0": earlier},
            }
        }
    )
    assert V2ManifestLoader(store).load().default_version == "5.1.0"

    manifest = publish_v2_manifest(store=store, worker=_cached_worker())

    assert sorted(manifest.versions) == ["5.1.0", "5.2.0"]
    assert V2ManifestLoader(store).load() == manifest


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
    with pytest.raises(
        ValueError, match=r"report_coordinator_callable\n  Field required"
    ):
        V2WorkerVersion.model_validate(value)


def test_cache_metadata_is_optional_for_old_workers_and_must_match_validation() -> None:
    old = _worker()
    assert old.cache is None

    cache = Stage12CacheRelease(
        manifest_sha256="c" * 64,
        partition_sha256="d" * 64,
        years=(2026, 2027, 2025),
    )
    value = old.model_dump(mode="json")
    value["cache"] = cache.model_dump(mode="json")
    with pytest.raises(ValueError, match="cache metadata differs"):
        V2WorkerVersion.model_validate(value)

    value = _worker().model_dump(mode="json")
    value["validation"]["validated"] = False
    with pytest.raises(
        ValueError, match=r"validation\.validated\n  Input should be True"
    ):
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


@pytest.mark.parametrize(
    ("country", "region", "region_type"),
    [
        ("us", None, "national"),
        ("us", "", "national"),
        ("us", " US ", "national"),
        ("us", "state/ut", "state"),
        ("us", "State/UT", "state"),
        ("us", "ut", "state"),
        ("us", " ut ", "state"),
        ("us", "congressional_district/ut-01", "congressional_district"),
        ("us", "CONGRESSIONAL_DISTRICT/UT-01", "congressional_district"),
        ("us", "place/UT-67000", "place"),
        ("us", "utah", None),
        ("uk", "uk", "national"),
        ("uk", "country/scotland", "country"),
        ("uk", "scotland", "country"),
        ("uk", "constituency/Aldershot", "constituency"),
    ],
)
def test_region_type_reads_codes_as_the_runtime_normalises_them(
    country: str, region: str | None, region_type: str | None
) -> None:
    assert region_type_of(country, region) == region_type


def test_a_country_bundle_gives_the_dataset_a_region_code_loads() -> None:
    worker = _regional_worker()
    us = next(country for country in worker.bundle.countries if country.country == "us")
    uk = next(country for country in worker.bundle.countries if country.country == "uk")

    assert us.dataset_for_region("State/UT") == "populace_us_2024_local"
    assert us.dataset_for_region("us") == "populace_us_2024"
    assert us.dataset_for_region("place/UT-67000") is None
    assert uk.dataset_for_region("country/scotland") is None
