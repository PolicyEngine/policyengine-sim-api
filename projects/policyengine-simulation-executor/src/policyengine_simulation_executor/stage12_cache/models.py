"""Strict models crossing Stage 12 precompute function boundaries."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictCacheModel(BaseModel):
    """Base for the cache models: unknown fields are rejected and values are frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CacheBundleIdentity(StrictCacheModel):
    """The installed bundle's US package, data and dataset versions."""

    bundle_manifest_sha256: str
    policyengine_version: str
    core_package_version: str
    country_package_version: str
    data_package_version: str
    data_artifact_revision: str
    default_dataset: str


class DatasetPlanEntry(StrictCacheModel):
    """One planned dataset year, its cache path and whether it is already stored."""

    year: int
    digest: str
    path: str
    filename: str
    exists: bool


class StateDatasetPlanEntry(StrictCacheModel):
    """One state's planned year file, filtered from a regional dataset.

    ``folder`` is where the worker image places the file, relative to its
    data folder; every state's file has the same ``filename``. ``exists``
    records whether the bucket already holds it.
    """

    year: int
    state_code: str
    region: str
    dataset: str
    digest: str
    path: str
    filename: str
    folder: str
    exists: bool


class BaselinePlanEntry(StrictCacheModel):
    """One planned baseline for a year and region group.

    ``filename`` is the name .py saves and loads; ``exists`` records whether
    the bucket already holds the file.
    """

    year: int
    segment_index: Annotated[int, Field(ge=0, lt=20)]
    region_codes: Annotated[tuple[str, ...], Field(min_length=1)]
    digest: str
    path: str
    filename: str
    simulation_id: str
    output_plan_sha256: str
    exists: bool


class PrecomputePlan(StrictCacheModel):
    """Every dataset and baseline the cache holds for the installed bundle.

    ``determinism_verified`` is true when the bucket holds a passing
    determinism record for exactly these baselines.
    """

    schema_version: Literal[1] = 1
    years: tuple[int, ...]
    datasets: tuple[DatasetPlanEntry, ...]
    baselines: tuple[BaselinePlanEntry, ...]
    bundle: CacheBundleIdentity
    partition_sha256: str
    determinism_verified: bool
    state_datasets: tuple[StateDatasetPlanEntry, ...] = ()


class WorkSelection(StrictCacheModel):
    """The planned datasets, state datasets and baselines a run still has to build."""

    datasets: tuple[DatasetPlanEntry, ...]
    baselines: tuple[BaselinePlanEntry, ...]
    state_datasets: tuple[StateDatasetPlanEntry, ...] = ()


class CacheObject(StrictCacheModel):
    """A stored cache object's path, content digest and size."""

    path: str
    content_sha256: str
    size_bytes: Annotated[int, Field(ge=0)]


class DatasetBuildResult(CacheObject):
    """A built dataset file and whether this run uploaded it."""

    year: int
    uploaded: bool
    build_seconds: float


class StateDatasetBuildResult(CacheObject):
    """A built state dataset file and whether this run uploaded it."""

    year: int
    state_code: str
    uploaded: bool


class BaselineBuildResult(CacheObject):
    """A computed baseline file and whether this run uploaded it."""

    year: int
    segment_index: int
    simulation_id: str
    cache_outcome: Literal["hit", "incomplete", "miss"] | None
    uploaded: bool
    compute_seconds: float


class DeterminismVerdict(StrictCacheModel):
    """Whether a stored baseline matched a fresh calculation.

    ``differences`` lists at most the first 50 differing entities or columns.
    """

    equal: bool
    differences: tuple[str, ...]


class ManifestArtifact(StrictCacheModel):
    """One dataset or baseline file listed in the cache manifest."""

    type: Literal["dataset", "baseline"]
    path: str
    filename: str
    year: int
    identity_digest: str
    content_sha256: str
    size_bytes: int


class StateDatasetManifestArtifact(StrictCacheModel):
    """One state's year file, which the worker image places in its state ``folder``.

    Its own model, so dataset and baseline entries keep their exact wire shape.
    """

    type: Literal["state_dataset"]
    path: str
    filename: str
    folder: str
    state_code: str
    year: int
    identity_digest: str
    content_sha256: str
    size_bytes: int


CacheManifestEntry = Annotated[
    ManifestArtifact | StateDatasetManifestArtifact, Field(discriminator="type")
]


class CacheManifest(StrictCacheModel):
    """The published list of cache files for one bundle and region partition."""

    manifest_schema: Literal["stage12-mf1"] = Field(
        default="stage12-mf1",
        alias="schema",
        serialization_alias="schema",
    )
    country: Literal["us"] = "us"
    years: tuple[int, ...]
    partition_sha256: str
    bundle: CacheBundleIdentity
    artifacts: tuple[CacheManifestEntry, ...]

    def canonical_payload(self) -> dict[str, Any]:
        """Return the JSON payload that is hashed and stored, keyed by ``schema``."""
        return self.model_dump(mode="json", by_alias=True)
