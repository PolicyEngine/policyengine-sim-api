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


class Stage12BundleManifest(StrictBundleModel):
    """Stage 12's normalized subset of a PolicyEngine.py bundle."""

    schema_version: Literal[1] = 1
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
