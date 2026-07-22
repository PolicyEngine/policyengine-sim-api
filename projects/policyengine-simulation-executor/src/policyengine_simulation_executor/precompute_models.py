"""Typed schemas for the precompute pipeline's data flow.

Every structure that crosses a function boundary in the precompute — the
plan, its entries, the bundle receipt, the deploy manifest, and the worker
results — is a strict Pydantic model (``extra="forbid"``), matching the
gateway contract's discipline. Plain dicts exist only at the Modal
serialization boundary: the app wrappers ``model_dump()`` on the way out
of a container and ``model_validate()`` on the way in, so a shape drift
between planner and worker fails loudly at the edge instead of surfacing
as a KeyError mid-computation.

Wire-compatibility constraint: ``ArtifactManifest.canonical_payload()``
must keep producing the exact key/value shape the store already holds —
the manifest is content-addressed, so any change here rotates published
digests. A regression test locks the digest of a fixed payload.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

# Mirrors baseline_artifacts.OUTCOME_*; the artifact outcome a worker
# observed when ensuring a baseline.
ArtifactOutcome = Literal["hit", "incomplete", "miss"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BundleReceipt(_StrictModel):
    """The version identity of the installed bundle a plan was made for."""

    policyengine_version: str
    model_version: str
    data_version: str
    data_artifact_revision: str
    default_dataset: str


class DatasetPlanEntry(_StrictModel):
    """One single-year dataset artifact: identity plus store presence."""

    year: int
    digest: str
    path: str
    filename: str
    exists: bool


class BaselinePlanEntry(_StrictModel):
    """One cohort baseline artifact: identity plus store presence."""

    year: int
    group: list[str]
    region: str
    digest: str
    path: str
    simulation_id: str
    exists: bool


class PrecomputePlan(_StrictModel):
    """Everything the store should contain for the installed bundle."""

    datasets: list[DatasetPlanEntry]
    baselines: list[BaselinePlanEntry]
    receipt: BundleReceipt


class WorkSelection(_StrictModel):
    """The subset of a plan that actually needs computing."""

    datasets: list[DatasetPlanEntry]
    baselines: list[BaselinePlanEntry]


class ManifestArtifact(_StrictModel):
    """One artifact the deploy image must fetch."""

    type: Literal["dataset", "baseline"]
    path: str
    filename: str
    year: int
    digest: str


class ArtifactManifest(_StrictModel):
    """The published deploy manifest (content-addressed in the store)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # "schema" is the wire key; the Python name avoids shadowing
    # BaseModel's deprecated .schema attribute.
    manifest_schema: str = Field(alias="schema")
    country: str
    receipt: BundleReceipt
    artifacts: list[ManifestArtifact]

    def canonical_payload(self) -> dict[str, Any]:
        """The exact dict shape that gets digested and stored."""
        return self.model_dump(by_alias=True)


class DatasetBuildResult(_StrictModel):
    year: int
    path: str
    uploaded: bool
    build_seconds: float
    size_bytes: int


class BaselineComputeResult(_StrictModel):
    year: int
    group: list[str]
    simulation_id: str
    outcome: Optional[ArtifactOutcome]
    uploaded: bool
    compute_seconds: float
    size_bytes: int


class DeterminismVerdict(_StrictModel):
    equal: bool
    differences: list[str]


class RemoteFunction(Protocol):
    """The slice of a Modal function handle the orchestration uses."""

    def remote(self, *args: Any) -> Any: ...

    def spawn(self, *args: Any) -> Any: ...
