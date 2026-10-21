"""Stage 12's per-state datasets: their cache identity and image folders.

Stage 12 keeps its own copy of the main path's per-state dataset machinery,
keyed from the published Stage 12 bundle.
"""

from __future__ import annotations

import dataclasses

import pytest
from policyengine_simulation_contract.stage12_bundle import (
    ResolvedStage12Bundle,
    Stage12BundleManifest,
    Stage12CountryBundle,
    Stage12Dataset,
)
from policyengine_simulation_executor.stage12_cache import state_datasets
from policyengine_simulation_executor.stage12_cache.keys import (
    Stage12StateDatasetCacheIdentity,
    collect_dataset_identity,
    collect_state_dataset_identity,
)

REGIONAL_DATASET = "populace_us_2024_acs_local"


def _resolved() -> ResolvedStage12Bundle:
    national = Stage12Dataset(
        identity="populace_us_2024",
        uri="hf://policyengine/populace-us/populace_us_2024.h5@national-revision",
        artifact_revision="national-revision",
        sha256="a" * 64,
        repo_type="dataset",
    )
    regional = Stage12Dataset(
        identity=REGIONAL_DATASET,
        uri=f"hf://policyengine/populace-us/{REGIONAL_DATASET}.h5@regional-revision",
        artifact_revision="regional-revision",
        sha256="c" * 64,
        repo_type="dataset",
    )
    country = Stage12CountryBundle(
        country="us",
        country_package_name="policyengine-us",
        country_package_version="1.0.0",
        country_package_requirement="policyengine-us==1.0.0",
        data_package_name="policyengine-us-data",
        data_package_version="2.0.0",
        data_release_version="2.0.0",
        data_artifact_revision="national-revision",
        default_dataset=national.identity,
        default_dataset_uri=national.uri,
        datasets=(national, regional),
    )
    return ResolvedStage12Bundle(
        bundle=Stage12BundleManifest(
            policyengine_version="5.2.0",
            policyengine_requirement="policyengine==5.2.0",
            core_package_version="3.0.0",
            core_package_requirement="policyengine-core==3.0.0",
            countries=(country,),
        ),
        bundle_manifest_sha256="b" * 64,
    )


def _utah_2026() -> Stage12StateDatasetCacheIdentity:
    return collect_state_dataset_identity(
        _resolved(),
        country="us",
        year=2026,
        dataset=REGIONAL_DATASET,
        state_code="ut",
        state_fips=49,
        filter_variable="state_fips",
    )


def test_identity_is_taken_from_the_regional_dataset_entry():
    identity = _utah_2026()

    assert identity.dataset == REGIONAL_DATASET
    assert identity.source_sha256 == "c" * 64
    assert identity.data_artifact_revision == "regional-revision"
    assert identity.state_code == "UT"
    assert identity.payload == {
        "schema": "stage12-sds1",
        "country": "us",
        "dataset": REGIONAL_DATASET,
        "year": 2026,
        "source_sha256": "c" * 64,
        "data_artifact_revision": "regional-revision",
        "data_package_version": "2.0.0",
        "country_package_version": "1.0.0",
        "core_package_version": "3.0.0",
        "policyengine_version": "5.2.0",
        "bundle_manifest_sha256": "b" * 64,
        "state_code": "UT",
        "state_fips": 49,
        "filter_variable": "state_fips",
    }


def test_file_keeps_the_year_file_name_under_its_own_store_path():
    identity = _utah_2026()

    assert identity.filename == f"{REGIONAL_DATASET}_year_2026.h5"
    assert identity.store_path == (
        f"state-datasets/us/{identity.digest}/{REGIONAL_DATASET}_year_2026.h5"
    )


@pytest.mark.parametrize(
    "field",
    [
        field.name
        for field in dataclasses.fields(Stage12StateDatasetCacheIdentity)
        if field.name not in {"stem", "country"}
    ],
)
def test_every_identity_field_changes_the_digest(field):
    identity = _utah_2026()
    replacement = {"year": 2027, "state_fips": 56}.get(field, "other")

    assert dataclasses.replace(identity, **{field: replacement}).digest != (
        identity.digest
    )


def test_refuses_a_dataset_missing_from_the_bundle():
    with pytest.raises(ValueError, match="populace_missing"):
        collect_state_dataset_identity(
            _resolved(),
            country="us",
            year=2026,
            dataset="populace_missing",
            state_code="UT",
            state_fips=49,
            filter_variable="state_fips",
        )


def test_the_national_dataset_identity_is_unchanged():
    identity = collect_dataset_identity(_resolved(), country="us", year=2026)

    assert identity.dataset == "populace_us_2024"
    assert identity.source_sha256 == "a" * 64
    assert identity.payload["schema"] == "stage12-ds1"


def test_state_folder_sits_under_the_stage12_data_folder():
    assert state_datasets.state_dataset_relative_folder(REGIONAL_DATASET, "ut") == (
        f"state-datasets/{REGIONAL_DATASET}/UT"
    )
    assert state_datasets.state_dataset_folder(
        "/opt/policyengine/stage12-data", REGIONAL_DATASET, "UT"
    ) == (f"/opt/policyengine/stage12-data/state-datasets/{REGIONAL_DATASET}/UT")
