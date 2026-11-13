"""Tests for the dataset a UK bundle gives constituency and local-authority regions."""

from __future__ import annotations

from policyengine_simulation_contract.stage12_bundle import (
    Stage12CountryBundle,
    Stage12Dataset,
    Stage12RegionDataset,
)

NATIONAL_DATASET = "microcosm_uk_2024_25"
LOCAL_AREA_DATASET = "microcosm_uk_2024_25_dense"


def _dataset(identity: str) -> Stage12Dataset:
    return Stage12Dataset(
        identity=identity,
        uri=f"hf://policyengine/data/{identity}.h5@{identity}-revision",
        artifact_revision=f"{identity}-revision",
        sha256="a" * 64,
        repo_type="model",
    )


def _uk_bundle(
    region_datasets: dict[str, str | None] | None,
) -> Stage12CountryBundle:
    return Stage12CountryBundle(
        country="uk",
        country_package_name="policyengine-uk",
        country_package_version="2.174.1",
        country_package_requirement="policyengine-uk==2.174.1",
        data_package_name="microcosm-uk-data",
        data_package_version="0.1.0",
        data_release_version="release",
        data_artifact_revision=f"{NATIONAL_DATASET}-revision",
        default_dataset=NATIONAL_DATASET,
        default_dataset_uri=_dataset(NATIONAL_DATASET).uri,
        datasets=(_dataset(NATIONAL_DATASET), _dataset(LOCAL_AREA_DATASET)),
        region_datasets=(
            None
            if region_datasets is None
            else tuple(
                Stage12RegionDataset(region_type=region_type, dataset=dataset)
                for region_type, dataset in region_datasets.items()
            )
        ),
    )


def test_both_area_region_types_on_their_own_dataset_name_the_local_area_dataset() -> (
    None
):
    bundle = _uk_bundle(
        {
            "national": NATIONAL_DATASET,
            "country": NATIONAL_DATASET,
            "constituency": LOCAL_AREA_DATASET,
            "local_authority": LOCAL_AREA_DATASET,
        }
    )

    assert bundle.local_area_dataset() == LOCAL_AREA_DATASET


def test_area_regions_on_the_national_dataset_have_no_local_area_dataset() -> None:
    bundle = _uk_bundle(
        {
            "national": NATIONAL_DATASET,
            "constituency": NATIONAL_DATASET,
            "local_authority": NATIONAL_DATASET,
        }
    )

    assert bundle.local_area_dataset() is None


def test_area_regions_on_two_datasets_have_no_local_area_dataset() -> None:
    bundle = _uk_bundle(
        {
            "national": NATIONAL_DATASET,
            "constituency": LOCAL_AREA_DATASET,
            "local_authority": "microcosm_uk_2024_25_authorities",
        }
    )

    assert bundle.local_area_dataset() is None


def test_an_area_region_type_without_one_dataset_has_no_local_area_dataset() -> None:
    bundle = _uk_bundle(
        {
            "national": NATIONAL_DATASET,
            "constituency": LOCAL_AREA_DATASET,
            "local_authority": None,
        }
    )

    assert bundle.local_area_dataset() is None


def test_a_bundle_published_before_region_datasets_has_no_local_area_dataset() -> None:
    assert _uk_bundle(None).local_area_dataset() is None
