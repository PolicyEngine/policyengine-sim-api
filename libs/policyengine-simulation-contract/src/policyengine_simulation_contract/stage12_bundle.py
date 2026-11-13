"""Normalized PolicyEngine.py bundle values shared by Stage 12 services."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

NonEmptyText = Annotated[str, Field(min_length=1)]
Sha256Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CountryId = Literal["us", "uk"]
SUPPORTED_COUNTRIES: tuple[CountryId, ...] = ("us", "uk")
# A bare two-letter US region is a state code, such as "ut".
_US_STATE_CODE_LENGTH = 2


def region_type_of(country: str, region: str | None) -> str | None:
    """Return the region type a requested region code names, as bundles key it.

    Codes are read as the simulation runtime normalises them: surrounding
    whitespace is ignored and the country itself, or no region, is
    ``national``. A bare two-letter US code is a state and a bare UK name is
    a UK country. Otherwise the type is the part before the first slash, in
    lower case: ``State/UT`` is ``state`` and ``congressional_district/UT-01``
    is ``congressional_district``. None means the code names no region type.
    """
    raw = (region or "").strip()
    if not raw or raw.lower() == country:
        return "national"
    if raw.lower() in SUPPORTED_COUNTRIES:
        # Another country's code names no region of this one.
        return None
    if "/" in raw:
        return raw.split("/", 1)[0].lower()
    if country == "us" and len(raw) == _US_STATE_CODE_LENGTH:
        return "state"
    if country == "uk":
        return "country"
    return None


class StrictBundleModel(BaseModel):
    """Base for normalized bundle models; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Stage12Dataset(StrictBundleModel):
    """A dataset available in a country bundle.

    Gives its identity, URI, artifact revision, SHA-256 and repository type.
    """

    identity: NonEmptyText
    uri: NonEmptyText
    artifact_revision: NonEmptyText
    sha256: Sha256Digest
    repo_type: NonEmptyText


class Stage12RegionDataset(StrictBundleModel):
    """The dataset one type of region loads, such as ``state`` or ``national``.

    ``dataset`` is a dataset identity in the country's bundle, or None when the
    bundle gives a per-region file pattern instead of one dataset.
    """

    region_type: NonEmptyText
    dataset: NonEmptyText | None


class Stage12CountryBundle(StrictBundleModel):
    """One country's pinned model package, data release and datasets within a bundle.

    ``region_datasets`` names the dataset each type of region loads. It is None
    in bundles published before it existed.
    """

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
    region_datasets: tuple[Stage12RegionDataset, ...] | None = None

    def dataset_for_region_type(self, region_type: str | None) -> str | None:
        """Return the dataset identity ``region_type`` regions load, or None."""
        for item in self.region_datasets or ():
            if item.region_type == region_type:
                return item.dataset
        return None

    def dataset_for_region(self, region: str | None) -> str | None:
        """Return the dataset identity a requested region code loads, or None."""
        return self.dataset_for_region_type(region_type_of(self.country, region))


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
    """A normalized bundle manifest together with its SHA-256 digest."""

    bundle: Stage12BundleManifest
    bundle_manifest_sha256: Sha256Digest
