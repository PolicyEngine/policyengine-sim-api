"""Strict models crossing Stage 12 precompute function boundaries."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, Any

from pydantic import BaseModel, ConfigDict, Field


class StrictCacheModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CacheBundleIdentity(StrictCacheModel):
    bundle_manifest_sha256: str
    policyengine_version: str
    core_package_version: str
    country_package_version: str
    data_package_version: str
    data_artifact_revision: str
    default_dataset: str


class DatasetPlanEntry(StrictCacheModel):
    year: int
    digest: str
    path: str
    filename: str
    exists: bool


class BaselinePlanEntry(StrictCacheModel):
    year: int
    segment_index: Annotated[int, Field(ge=0, lt=20)]
    region_codes: Annotated[tuple[str, ...], Field(min_length=1)]
    digest: str
    path: str
    simulation_id: str
    output_plan_sha256: str
    exists: bool


class PrecomputePlan(StrictCacheModel):
    schema_version: Literal[1] = 1
    years: tuple[int, ...]
    datasets: tuple[DatasetPlanEntry, ...]
    baselines: tuple[BaselinePlanEntry, ...]
    bundle: CacheBundleIdentity
    partition_sha256: str


class WorkSelection(StrictCacheModel):
    datasets: tuple[DatasetPlanEntry, ...]
    baselines: tuple[BaselinePlanEntry, ...]


class CacheObject(StrictCacheModel):
    path: str
    content_sha256: str
    size_bytes: Annotated[int, Field(ge=0)]


class DatasetBuildResult(CacheObject):
    year: int
    uploaded: bool
    build_seconds: float


class BaselineBuildResult(CacheObject):
    year: int
    segment_index: int
    simulation_id: str
    cache_outcome: Literal["hit", "incomplete", "miss"] | None
    uploaded: bool
    compute_seconds: float


class DeterminismVerdict(StrictCacheModel):
    equal: bool
    differences: tuple[str, ...]


class ManifestArtifact(StrictCacheModel):
    type: Literal["dataset", "baseline"]
    path: str
    filename: str
    year: int
    identity_digest: str
    content_sha256: str
    size_bytes: int


class CacheManifest(StrictCacheModel):
    manifest_schema: Literal["stage12-mf1"] = Field(
        default="stage12-mf1",
        alias="schema",
        serialization_alias="schema",
    )
    country: Literal["us"] = "us"
    years: tuple[int, ...]
    partition_sha256: str
    bundle: CacheBundleIdentity
    artifacts: tuple[ManifestArtifact, ...]

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class RemoteFunction(Protocol):
    def remote(self, *args: Any) -> Any: ...

    def spawn(self, *args: Any) -> Any: ...
