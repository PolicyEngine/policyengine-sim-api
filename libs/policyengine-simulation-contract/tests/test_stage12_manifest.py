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
    SCHEMA_2_MANIFEST_KEY,
    V1_ROUTING_STATE_NAME,
    V2_VERSION_MANIFEST_NAME,
    Stage12CacheRelease,
    V2CountryWorker,
    V2ManifestLoader,
    V2VersionManifest,
    V2WorkerValidation,
    V2WorkerVersion,
    assert_separate_manifest_names,
    manifest_digest,
    parse_v2_manifest,
    pre_cache_manifest_payload,
    pre_region_datasets_manifest_payload,
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


def _country_worker(
    country: str, *, economy: bool = True, household: bool = False
) -> V2CountryWorker:
    return V2CountryWorker(
        country=country,
        economy_capable=economy,
        single_simulation_callable=(
            f"run_single_simulation_{country}" if economy else None
        ),
        household_callable=f"calculate_household_{country}" if household else None,
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
        _country_worker(
            country,
            economy=country not in household_only,
            household=with_household_functions or country in household_only,
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
                if worker.economy_capable
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


def _regional_worker(
    version: str = "5.2.0", *, with_household_functions: bool = False
) -> V2WorkerVersion:
    """Return a cached worker whose US bundle names each region type's dataset."""
    worker = _cached_worker(version, with_household_functions=with_household_functions)
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

    manifest = publish_v2_manifest(
        store=store, worker=_regional_worker(with_household_functions=True)
    )

    assert store.writes == [
        (SCHEMA_2_MANIFEST_KEY, manifest.model_dump(mode="json")),
        (REGION_DATASETS_MANIFEST_KEY, schema_1_manifest_payload(manifest)),
        (ACTIVE_MANIFEST_KEY, pre_region_datasets_manifest_payload(manifest)),
        (PRE_CACHE_MANIFEST_KEY, pre_cache_manifest_payload(manifest)),
    ]
    assert manifest.schema_version == 2
    assert manifest.default_version == "5.2.0"
    assert manifest.versions["5.2.0"].validation.validated is True
    loaded = V2ManifestLoader(store).load()
    assert loaded == manifest
    assert loaded.versions["5.2.0"].cache is not None
    assert loaded.versions["5.2.0"].household_worker("us") is not None


def _assert_schema_1_top_level(payload: object) -> dict:
    # The fields of the schema version 1 manifest and country models. Readers
    # released before schema version 2 forbid any other field, so adding one
    # here would stop them loading.
    assert isinstance(payload, dict)
    assert payload["schema_version"] == 1
    assert set(payload) == {
        "default_version",
        "generation",
        "schema_version",
        "versions",
    }
    for country in payload["versions"]["5.2.0"]["countries"]:
        assert set(country) == {"country", "single_simulation_callable"}
    return payload


@pytest.mark.parametrize("key", [REGION_DATASETS_MANIFEST_KEY, ACTIVE_MANIFEST_KEY])
def test_readers_released_before_schema_2_get_only_fields_they_accept(
    key: str,
) -> None:
    store = FakeStore()
    publish_v2_manifest(
        store=store, worker=_regional_worker(with_household_functions=True)
    )

    with_cache = _assert_schema_1_top_level(store.values[key])
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
    pre_cache = _assert_schema_1_top_level(store.values[PRE_CACHE_MANIFEST_KEY])
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


@pytest.mark.parametrize("key", [SCHEMA_2_MANIFEST_KEY, REGION_DATASETS_MANIFEST_KEY])
def test_the_region_dataset_keys_keep_region_datasets_and_cache(key: str) -> None:
    store = FakeStore()
    publish_v2_manifest(store=store, worker=_regional_worker())

    loaded = parse_v2_manifest(store.values[key]).versions["5.2.0"]
    us = next(country for country in loaded.bundle.countries if country.country == "us")
    assert us.dataset_for_region_type("state") == "populace_us_2024_local"
    assert us.dataset_for_region_type("national") == "populace_us_2024"
    assert us.dataset_for_region_type("congressional_district") is None
    assert loaded.cache is not None
    assert V2ManifestLoader(store).load().versions["5.2.0"] == loaded


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


def _schema_1_document(*, include_cache: bool, version: str = "5.1.0") -> dict:
    """Return a manifest exactly as a publisher released before schema 2 wrote it."""
    worker = _worker(version).model_dump(
        mode="json",
        exclude={
            "countries": {"__all__": {"economy_capable", "household_callable"}},
            "validation": (
                {"household_validation_invocation_ids"}
                | (set() if include_cache else {"cache"})
            ),
            **({} if include_cache else {"cache": True}),
        },
    )
    return {
        "schema_version": 1,
        "generation": f"{version}:earlier",
        "default_version": version,
        "versions": {version: worker},
    }


@pytest.mark.parametrize(
    ("key", "include_cache"),
    [(ACTIVE_MANIFEST_KEY, True), (PRE_CACHE_MANIFEST_KEY, False)],
)
def test_loader_reads_a_schema_1_document_as_economy_capable_countries(
    *, key: str, include_cache: bool
) -> None:
    store = FakeStore({key: _schema_1_document(include_cache=include_cache)})

    manifest = V2ManifestLoader(store).load()

    assert manifest.schema_version == 2
    worker = manifest.versions["5.1.0"]
    assert [country.economy_capable for country in worker.countries] == [True, True]
    assert worker.economy_worker("uk") == _country_worker("uk")
    assert worker.household_worker("us") is None
    assert worker.validation.household_validation_invocation_ids == {}


def test_loader_reads_the_schema_2_document_its_publish_wrote() -> None:
    store = FakeStore({ACTIVE_MANIFEST_KEY: _schema_1_document(include_cache=True)})
    newer = publish_v2_manifest(
        store=store, worker=_worker(with_household_functions=True)
    )

    loaded = V2ManifestLoader(store).load()

    # Only the schema version 2 document names the household functions.
    assert loaded == newer
    assert loaded.versions["5.2.0"].household_worker("us") is not None


@pytest.mark.parametrize(
    ("key", "include_cache"),
    [(ACTIVE_MANIFEST_KEY, True), (PRE_CACHE_MANIFEST_KEY, False)],
)
def test_first_schema_2_publish_keeps_versions_from_a_schema_1_key(
    *, key: str, include_cache: bool
) -> None:
    store = FakeStore({key: _schema_1_document(include_cache=include_cache)})
    assert V2ManifestLoader(store).load().default_version == "5.1.0"

    manifest = publish_v2_manifest(
        store=store, worker=_cached_worker(with_household_functions=True)
    )

    assert sorted(manifest.versions) == ["5.1.0", "5.2.0"]
    assert manifest.versions["5.1.0"].household_worker("us") is None
    assert manifest.versions["5.2.0"].household_worker("us") is not None
    assert V2ManifestLoader(store).load() == manifest


def test_a_household_only_country_leaves_schema_1_readers_on_their_manifest() -> None:
    earlier = _schema_1_document(include_cache=True)
    store = FakeStore({ACTIVE_MANIFEST_KEY: earlier})

    manifest = publish_v2_manifest(store=store, worker=_worker(household_only=("uk",)))

    # Schema version 1 cannot describe a country without a single-simulation
    # function, so only the schema version 2 document changes.
    assert [key for key, _ in store.writes] == [SCHEMA_2_MANIFEST_KEY]
    assert store.values[ACTIVE_MANIFEST_KEY] == earlier
    assert schema_1_manifest_payload(manifest, include_cache=True) is None
    worker = manifest.versions["5.2.0"]
    assert worker.economy_worker("uk") is None
    assert worker.household_worker("uk") is not None
    assert worker.economy_worker("us") is not None


def _validated_on(worker: V2WorkerVersion, day: int) -> V2WorkerVersion:
    """Return ``worker`` as validated on ``day`` of September 2026."""
    validation = worker.validation.model_copy(
        update={"validated_at": datetime(2026, 9, day, tzinfo=UTC)}
    )
    return worker.model_copy(update={"validation": validation})


def _write_as_a_schema_1_publisher(
    store: FakeStore, *workers: V2WorkerVersion, default_version: str
) -> None:
    """Write the older keys as a publisher released before schema 2 writes them."""
    manifest = V2VersionManifest(
        generation="older-publisher",
        default_version=default_version,
        versions={worker.bundle.policyengine_version: worker for worker in workers},
    )
    store[ACTIVE_MANIFEST_KEY] = schema_1_manifest_payload(manifest, include_cache=True)
    store[PRE_CACHE_MANIFEST_KEY] = schema_1_manifest_payload(
        manifest, include_cache=False
    )


def test_a_publish_keeps_what_an_older_publisher_wrote_after_it() -> None:
    # A deployment of code released before schema 2 (for example after a
    # revert) redeploys 5.2.0 without household functions and writes only the
    # older keys. The next schema 2 publish must start from that, not from
    # its own stale document.
    store = FakeStore()
    publish_v2_manifest(
        store=store,
        worker=_validated_on(_worker(with_household_functions=True), 14),
    )
    _write_as_a_schema_1_publisher(
        store, _validated_on(_worker(), 20), default_version="5.2.0"
    )

    manifest = publish_v2_manifest(
        store=store,
        worker=_validated_on(_worker("5.3.0", with_household_functions=True), 25),
    )

    assert sorted(manifest.versions) == ["5.2.0", "5.3.0"]
    assert manifest.versions["5.2.0"].household_worker("us") is None
    assert manifest.versions["5.2.0"].validation.validated_at.day == 20
    assert manifest.default_version == "5.3.0"


def test_a_publish_keeps_the_default_of_the_latest_document() -> None:
    store = FakeStore()
    publish_v2_manifest(store=store, worker=_validated_on(_worker("5.3.0"), 14))
    # An older publisher later made 5.2.0 the default.
    _write_as_a_schema_1_publisher(
        store,
        _validated_on(_worker("5.3.0"), 14),
        _validated_on(_worker("5.2.0"), 20),
        default_version="5.2.0",
    )

    manifest = publish_v2_manifest(
        store=store, worker=_validated_on(_worker("5.2.5"), 25)
    )

    # 5.2.5 is newer than the latest default, 5.2.0, so it becomes the default.
    assert manifest.default_version == "5.2.5"
    assert sorted(manifest.versions) == ["5.2.0", "5.2.5", "5.3.0"]


def test_older_keys_left_behind_by_a_schema_2_publish_change_nothing() -> None:
    store = FakeStore({ACTIVE_MANIFEST_KEY: _schema_1_document(include_cache=True)})
    publish_v2_manifest(
        store=store, worker=_validated_on(_worker(household_only=("uk",)), 14)
    )

    manifest = publish_v2_manifest(
        store=store,
        worker=_validated_on(_worker("5.3.0", household_only=("uk",)), 25),
    )

    assert sorted(manifest.versions) == ["5.1.0", "5.2.0", "5.3.0"]
    assert manifest.versions["5.2.0"].economy_worker("uk") is None
    assert manifest.default_version == "5.3.0"


def test_only_an_economy_capable_country_names_a_simulation_callable() -> None:
    worker = _worker(household_only=("uk",))

    assert worker.economy_simulation_callable("us") == "run_single_simulation_us"
    with pytest.raises(LookupError, match="'uk' is not economy-capable"):
        worker.economy_simulation_callable("uk")
    with pytest.raises(LookupError, match="'fr' is not economy-capable"):
        worker.economy_simulation_callable("fr")


def test_parse_v2_manifest_rejects_unknown_schema_versions() -> None:
    document = _schema_1_document(include_cache=True)
    document["schema_version"] = 3

    with pytest.raises(ValueError, match="schema_version"):
        parse_v2_manifest(document)


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


@pytest.mark.parametrize(
    ("economy_capable", "single_simulation_callable", "household_callable"),
    [
        (True, None, None),
        (False, "run_single_simulation_us", None),
        (False, "run_single_simulation_us", "calculate_household_us"),
        (False, None, None),
    ],
)
def test_country_capabilities_must_name_their_functions(
    *,
    economy_capable: bool,
    single_simulation_callable: str | None,
    household_callable: str | None,
) -> None:
    with pytest.raises(ValueError, match=r"economy-capable|capability"):
        V2CountryWorker(
            country="us",
            economy_capable=economy_capable,
            single_simulation_callable=single_simulation_callable,
            household_callable=household_callable,
        )


def test_household_functions_need_a_validation_receipt() -> None:
    value = _worker(with_household_functions=True).model_dump(mode="json")
    value["validation"]["household_validation_invocation_ids"] = {"us": "only-us"}

    with pytest.raises(ValueError, match="household"):
        V2WorkerVersion.model_validate(value)


def test_only_economy_capable_countries_have_simulation_receipts() -> None:
    value = _worker(household_only=("uk",)).model_dump(mode="json")
    value["validation"]["country_validation_invocation_ids"]["uk"] = "not-needed"

    with pytest.raises(ValueError, match="economy-capable"):
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
        ("us", "uk", None),
        ("uk", "us", None),
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


def test_a_region_dataset_copy_left_behind_by_an_older_publish_is_not_read() -> None:
    """After a rollback, older code republishes only the keys it knows."""
    store = FakeStore()
    publish_v2_manifest(store=store, worker=_regional_worker("5.2.0"))
    stale = store.values[REGION_DATASETS_MANIFEST_KEY]
    # A publish by code released before region datasets: it writes the two
    # older keys only, leaving the region dataset copy as it was.
    older = publish_v2_manifest(store=FakeStore(), worker=_cached_worker("5.3.0"))
    store.values[ACTIVE_MANIFEST_KEY] = pre_region_datasets_manifest_payload(older)
    store.values[PRE_CACHE_MANIFEST_KEY] = pre_cache_manifest_payload(older)

    loaded = V2ManifestLoader(store).load()

    assert loaded.default_version == "5.3.0"
    assert store.values[REGION_DATASETS_MANIFEST_KEY] == stale


def test_a_publish_after_a_rollback_keeps_the_versions_the_older_code_added() -> None:
    store = FakeStore()
    publish_v2_manifest(store=store, worker=_regional_worker("5.2.0"))
    older = publish_v2_manifest(store=FakeStore(), worker=_cached_worker("5.3.0"))
    store.values[ACTIVE_MANIFEST_KEY] = pre_region_datasets_manifest_payload(older)

    manifest = publish_v2_manifest(store=store, worker=_regional_worker("5.4.0"))

    assert sorted(manifest.versions) == ["5.3.0", "5.4.0"]


def test_a_schema_2_document_left_behind_by_an_older_publish_is_not_read() -> None:
    """After a rollback, code released before schema 2 writes only older keys."""
    store = FakeStore()
    publish_v2_manifest(
        store=store, worker=_regional_worker(with_household_functions=True)
    )
    stale = store.values[SCHEMA_2_MANIFEST_KEY]
    older = publish_v2_manifest(store=FakeStore(), worker=_regional_worker("5.3.0"))
    store.values[REGION_DATASETS_MANIFEST_KEY] = schema_1_manifest_payload(older)
    store.values[ACTIVE_MANIFEST_KEY] = pre_region_datasets_manifest_payload(older)
    store.values[PRE_CACHE_MANIFEST_KEY] = pre_cache_manifest_payload(older)

    loaded = V2ManifestLoader(store).load()
    manifest = publish_v2_manifest(store=store, worker=_regional_worker("5.4.0"))

    assert loaded.default_version == "5.3.0"
    assert loaded.versions["5.3.0"].household_worker("us") is None
    assert sorted(manifest.versions) == ["5.3.0", "5.4.0"]
    assert store.values[SCHEMA_2_MANIFEST_KEY] != stale


def test_a_household_only_manifest_is_read_until_an_older_publish_follows() -> None:
    # Schema version 1 cannot describe a household-only country, so its
    # publish leaves the older keys alone; a later validation under them
    # means code released before schema 2 published after it.
    store = FakeStore({ACTIVE_MANIFEST_KEY: _schema_1_document(include_cache=True)})
    household_only = publish_v2_manifest(
        store=store, worker=_validated_on(_worker(household_only=("uk",)), 15)
    )
    assert V2ManifestLoader(store).load() == household_only

    older = publish_v2_manifest(
        store=FakeStore(), worker=_validated_on(_cached_worker("5.3.0"), 20)
    )
    store.values[ACTIVE_MANIFEST_KEY] = pre_region_datasets_manifest_payload(older)

    assert V2ManifestLoader(store).load().default_version == "5.3.0"
