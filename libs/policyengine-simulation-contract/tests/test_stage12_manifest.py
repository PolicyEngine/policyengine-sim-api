"""Tests for the separate Stage 12 v2 worker manifest."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from policyengine_simulation_contract.stage12_bundle import (
    ResolvedStage12Bundle,
    Stage12BundleManifest,
    Stage12CountryBundle,
    Stage12Dataset,
)
from policyengine_simulation_contract.stage12_manifest import (
    ACTIVE_MANIFEST_KEY,
    PRE_CACHE_MANIFEST_KEY,
    SCHEMA_2_MANIFEST_KEY,
    V1_ROUTING_STATE_NAME,
    V2_VERSION_MANIFEST_NAME,
    Stage12CacheRelease,
    V2CountryWorker,
    V2ManifestLoader,
    V2WorkerValidation,
    V2WorkerVersion,
    assert_separate_manifest_names,
    manifest_digest,
    parse_v2_manifest,
    publish_v2_manifest,
    schema_1_manifest_payload,
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


def _worker(
    version: str = "5.2.0",
    *,
    with_household_functions: bool = False,
    household_only: tuple[str, ...] = (),
) -> V2WorkerVersion:
    resolved = _resolved_bundle(version)
    application_name = v2_application_name(version)
    countries = tuple(
        V2CountryWorker(
            country=country,
            society_wide_capable=country not in household_only,
            single_simulation_callable=(
                None
                if country in household_only
                else f"run_single_simulation_{country}"
            ),
            household_callable=(
                f"calculate_household_{country}"
                if with_household_functions or country in household_only
                else None
            ),
        )
        for country in ("us", "uk")
    )
    return V2WorkerVersion(
        application_name=application_name,
        report_coordinator_callable="coordinate_report",
        countries=countries,
        bundle=resolved.bundle,
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        validation=V2WorkerValidation(
            validated=True,
            application_name=application_name,
            bundle_manifest_sha256=resolved.bundle_manifest_sha256,
            validated_at=datetime(2026, 9, 14, tzinfo=UTC),
            validation_invocation_id="validation-123",
            country_validation_invocation_ids={
                worker.country: f"validation-{worker.country}-123"
                for worker in countries
                if worker.society_wide_capable
            },
            household_validation_invocation_ids={
                worker.country: f"household-validation-{worker.country}-123"
                for worker in countries
                if worker.household_callable is not None
            },
        ),
    )


def test_v2_application_name_cannot_collide_with_v1() -> None:
    assert v2_application_name("5.2.0") == "policyengine-simulation-v2-py5-2-0"
    assert not v2_application_name("5.2.0").startswith("policyengine-simulation-py")


def _cached_worker(
    version: str = "5.2.0", *, with_household_functions: bool = False
) -> V2WorkerVersion:
    worker = _worker(version, with_household_functions=with_household_functions)
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


def test_publish_writes_one_complete_v2_document_per_key() -> None:
    store = FakeStore()

    manifest = publish_v2_manifest(
        store=store, worker=_cached_worker(with_household_functions=True)
    )

    assert store.writes == [
        (SCHEMA_2_MANIFEST_KEY, manifest.model_dump(mode="json")),
        (
            ACTIVE_MANIFEST_KEY,
            schema_1_manifest_payload(manifest, include_cache=True),
        ),
        (
            PRE_CACHE_MANIFEST_KEY,
            schema_1_manifest_payload(manifest, include_cache=False),
        ),
    ]
    assert manifest.schema_version == 2
    assert manifest.default_version == "5.2.0"
    assert manifest.versions["5.2.0"].validation.validated is True
    loaded = V2ManifestLoader(store).load()
    assert loaded == manifest
    assert loaded.versions["5.2.0"].cache is not None
    assert loaded.versions["5.2.0"].household_worker("us") is not None


def _schema_1_fields(payload: dict) -> None:
    # The fields of each schema version 1 model. Readers released before
    # schema version 2 forbid any other field, so adding one here would stop
    # them loading.
    assert payload["schema_version"] == 1
    assert set(payload) == {
        "default_version",
        "generation",
        "schema_version",
        "versions",
    }
    for country in payload["versions"]["5.2.0"]["countries"]:
        assert set(country) == {"country", "single_simulation_callable"}


def test_readers_released_before_schema_2_get_only_fields_they_accept() -> None:
    store = FakeStore()
    publish_v2_manifest(
        store=store, worker=_cached_worker(with_household_functions=True)
    )

    with_cache = store.values[ACTIVE_MANIFEST_KEY]
    _schema_1_fields(with_cache)
    worker = with_cache["versions"]["5.2.0"]
    assert set(worker) == {
        "application_name",
        "bundle",
        "bundle_manifest_sha256",
        "cache",
        "countries",
        "report_coordinator_callable",
        "validation",
    }
    assert set(worker["validation"]) == {
        "application_name",
        "bundle_manifest_sha256",
        "cache",
        "country_validation_invocation_ids",
        "validated",
        "validated_at",
        "validation_invocation_id",
    }


def test_readers_released_before_the_cache_get_only_fields_they_accept() -> None:
    store = FakeStore()
    publish_v2_manifest(
        store=store, worker=_cached_worker(with_household_functions=True)
    )

    # The fields of each model before Stage 12 cache releases. Those readers
    # forbid any other field, so adding one here would stop them loading.
    pre_cache = store.values[PRE_CACHE_MANIFEST_KEY]
    _schema_1_fields(pre_cache)
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


def _schema_1_store(key: str, version: str = "5.1.0") -> FakeStore:
    earlier = schema_1_manifest_payload(
        parse_v2_manifest(
            {
                "schema_version": 2,
                "generation": f"{version}:earlier",
                "default_version": version,
                "versions": {version: _worker(version).model_dump(mode="json")},
            }
        ),
        include_cache=key == ACTIVE_MANIFEST_KEY,
    )
    return FakeStore({key: earlier})


@pytest.mark.parametrize("key", [ACTIVE_MANIFEST_KEY, PRE_CACHE_MANIFEST_KEY])
def test_loader_upgrades_a_schema_1_document(key: str) -> None:
    store = _schema_1_store(key)

    manifest = V2ManifestLoader(store).load()

    assert manifest.schema_version == 2
    worker = manifest.versions["5.1.0"]
    assert [country.society_wide_capable for country in worker.countries] == [
        True,
        True,
    ]
    assert worker.household_worker("us") is None
    assert worker.society_wide_worker("uk") is not None
    assert worker.validation.household_validation_invocation_ids == {}


def test_loader_prefers_the_schema_2_document() -> None:
    store = _schema_1_store(ACTIVE_MANIFEST_KEY)
    newer = publish_v2_manifest(
        store=FakeStore(), worker=_worker(with_household_functions=True)
    )
    store.values[SCHEMA_2_MANIFEST_KEY] = newer.model_dump(mode="json")

    assert V2ManifestLoader(store).load() == newer


@pytest.mark.parametrize("key", [ACTIVE_MANIFEST_KEY, PRE_CACHE_MANIFEST_KEY])
def test_first_schema_2_publish_keeps_versions_from_a_schema_1_key(key: str) -> None:
    store = _schema_1_store(key)
    assert V2ManifestLoader(store).load().default_version == "5.1.0"

    manifest = publish_v2_manifest(
        store=store, worker=_cached_worker(with_household_functions=True)
    )

    assert sorted(manifest.versions) == ["5.1.0", "5.2.0"]
    assert manifest.versions["5.1.0"].household_worker("us") is None
    assert manifest.versions["5.2.0"].household_worker("us") is not None
    assert V2ManifestLoader(store).load() == manifest


def test_household_only_country_keeps_schema_1_readers_on_their_last_manifest() -> None:
    store = _schema_1_store(ACTIVE_MANIFEST_KEY, version="5.1.0")
    legacy_before = store.values[ACTIVE_MANIFEST_KEY]

    manifest = publish_v2_manifest(store=store, worker=_worker(household_only=("uk",)))

    # Schema version 1 cannot describe a country without a single-simulation
    # function, so only the schema version 2 document changes.
    assert [key for key, _ in store.writes] == [SCHEMA_2_MANIFEST_KEY]
    assert store.values[ACTIVE_MANIFEST_KEY] == legacy_before
    assert schema_1_manifest_payload(manifest, include_cache=True) is None
    worker = manifest.versions["5.2.0"]
    assert worker.society_wide_worker("uk") is None
    assert worker.household_worker("uk") is not None
    assert worker.society_wide_worker("us") is not None


def test_loader_retains_its_last_valid_v2_document() -> None:
    store = FakeStore()
    expected = publish_v2_manifest(store=store, worker=_worker())
    loader = V2ManifestLoader(store)
    assert loader.load() == expected

    store.values[SCHEMA_2_MANIFEST_KEY] = {"schema_version": 999}

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

    v2_store.values.pop(SCHEMA_2_MANIFEST_KEY)
    assert v1_store.values == {ACTIVE_MANIFEST_KEY: {"v1": "new"}}


def test_manifest_rejects_incomplete_or_unvalidated_worker() -> None:
    value = _worker().model_dump(mode="json")
    value.pop("report_coordinator_callable")
    with pytest.raises(ValueError):
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
    with pytest.raises(ValueError):
        V2WorkerVersion.model_validate(value)


@pytest.mark.parametrize(
    ("society_wide_capable", "single_simulation_callable", "household_callable"),
    [
        (True, None, None),
        (False, "run_single_simulation_us", None),
        (False, None, None),
    ],
)
def test_country_capabilities_must_name_their_functions(
    society_wide_capable: bool,
    single_simulation_callable: str | None,
    household_callable: str | None,
) -> None:
    with pytest.raises(ValueError, match="society-wide|capability"):
        V2CountryWorker(
            country="us",
            society_wide_capable=society_wide_capable,
            single_simulation_callable=single_simulation_callable,
            household_callable=household_callable,
        )


def test_household_functions_need_a_validation_receipt() -> None:
    value = _worker(with_household_functions=True).model_dump(mode="json")
    value["validation"]["household_validation_invocation_ids"] = {"us": "only-us"}

    with pytest.raises(ValueError, match="household"):
        V2WorkerVersion.model_validate(value)


def test_society_wide_countries_need_a_validation_receipt() -> None:
    value = _worker(household_only=("uk",)).model_dump(mode="json")
    value["validation"]["country_validation_invocation_ids"]["uk"] = "not-needed"

    with pytest.raises(ValueError, match="society-wide"):
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
