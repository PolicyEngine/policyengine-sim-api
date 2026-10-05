"""Normalized PolicyEngine.py bundle values shared by Stage 12 services."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

NonEmptyText = Annotated[str, Field(min_length=1)]
Sha256Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CountryId = Literal["us", "uk"]
SUPPORTED_COUNTRIES: tuple[CountryId, ...] = ("us", "uk")


class StrictBundleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Stage12Dataset(StrictBundleModel):
    identity: NonEmptyText
    uri: NonEmptyText
    artifact_revision: NonEmptyText
    sha256: Sha256Digest
    repo_type: NonEmptyText


class Stage12CountryBundle(StrictBundleModel):
    country: CountryId
    country_package_name: NonEmptyText
    country_package_version: NonEmptyText
    country_package_requirement: NonEmptyText
    data_package_name: NonEmptyText
    data_package_version: NonEmptyText
    data_release_version: NonEmptyText
    data_artifact_revision: NonEmptyText
    default_dataset: NonEmptyText
    default_dataset_uri: NonEmptyText
    datasets: tuple[Stage12Dataset, ...]
    regional_dataset_defaults: dict[NonEmptyText, NonEmptyText] = Field(
        default_factory=dict
    )


class Stage12BundleManifest(StrictBundleModel):
    """Stage 12's normalized subset of a PolicyEngine.py bundle."""

    schema_version: Literal[2] = 2
    source_bundle_schema_version: Literal[2] = 2
    policyengine_version: NonEmptyText
    policyengine_requirement: NonEmptyText
    core_package_name: Literal["policyengine-core"] = "policyengine-core"
    core_package_version: NonEmptyText
    core_package_requirement: NonEmptyText
    countries: tuple[Stage12CountryBundle, ...]


class ResolvedStage12Bundle(StrictBundleModel):
    bundle: Stage12BundleManifest
    bundle_manifest_sha256: Sha256Digest


def select_stage12_dataset(
    country: Stage12CountryBundle,
    region: str,
) -> Stage12Dataset:
    """Select the certified dataset declared for a concrete region type."""

    normalized_region = region.strip().lower()
    dataset_identity = country.default_dataset
    if normalized_region != country.country:
        if (
            country.country == "us"
            and len(normalized_region) == 2
            and normalized_region.isalpha()
        ):
            dataset_identity = country.regional_dataset_defaults.get(
                "state", country.default_dataset
            )
        else:
            region_type, separator, _ = normalized_region.partition("/")
            if separator:
                dataset_identity = country.regional_dataset_defaults.get(
                    region_type, country.default_dataset
                )
    return next(
        dataset for dataset in country.datasets if dataset.identity == dataset_identity
    )
