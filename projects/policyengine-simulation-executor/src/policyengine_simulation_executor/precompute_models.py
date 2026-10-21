"""Typed schemas for the precompute pipeline's data flow.

Every structure that crosses a function boundary in the precompute — the
plan, its entries, the bundle version identity, the deploy manifest, the
deployed-environment marker, and the worker results — is a strict
Pydantic model (``extra="forbid"``),
matching the gateway contract's discipline. Plain dicts exist only at the
Modal serialization boundary: wherever a structured value crosses, the
app wrappers ``model_dump()`` on the way out of a container and
``model_validate()`` on the way in (bare strings — the bucket, the
manifest digest — cross as-is), so a shape drift between planner and
worker fails loudly at the edge instead of surfacing as a KeyError
mid-computation.

Wire-compatibility constraint: ``ArtifactManifest.canonical_payload()``
must keep producing the exact key/value shape the store already holds —
the manifest is content-addressed, so any change here rotates published
digests. A regression test locks the digest of a fixed payload.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

# Mirrors baseline_artifacts.OUTCOME_*; the artifact outcome a worker
# observed when ensuring a baseline.
ArtifactOutcome = Literal["hit", "incomplete", "miss"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BundleVersionIdentity(_StrictModel):
    """The version identity of the installed bundle a plan was made for.

    Travels under the frozen wire key ``receipt``, but is deliberately NOT
    the install receipt file (``.policyengine-bundle-receipt.json``): that
    carries per-dataset content hashes, which live inside the artifact
    digests here, not in this version tuple.
    """

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


class StateDatasetPlanEntry(_StrictModel):
    """One state's year file filtered from a regional dataset: identity plus presence.

    ``folder`` is where the image places the file, relative to its data folder;
    every state's file has the same ``filename``.
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
    receipt: BundleVersionIdentity
    state_datasets: list[StateDatasetPlanEntry] = Field(default_factory=list)


class WorkSelection(_StrictModel):
    """The subset of a plan that actually needs computing."""

    datasets: list[DatasetPlanEntry]
    baselines: list[BaselinePlanEntry]
    state_datasets: list[StateDatasetPlanEntry] = Field(default_factory=list)


class ManifestArtifact(_StrictModel):
    """One artifact the deploy image must fetch into its data folder."""

    type: Literal["dataset", "baseline"]
    path: str
    filename: str
    year: int
    digest: str


class StateDatasetArtifact(_StrictModel):
    """One state's year file, which the image fetches into the state's ``folder``.

    Its own model, rather than optional fields on ``ManifestArtifact``, so the
    dataset and baseline entries keep exactly their published wire shape.
    """

    type: Literal["state_dataset"]
    path: str
    filename: str
    folder: str
    state_code: str
    year: int
    digest: str


ManifestEntry = Annotated[
    ManifestArtifact | StateDatasetArtifact, Field(discriminator="type")
]


class ArtifactManifest(_StrictModel):
    """The published deploy manifest (content-addressed in the store)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # "schema" is the wire key; the Python name avoids shadowing
    # BaseModel's deprecated .schema attribute.
    manifest_schema: str = Field(alias="schema")
    country: str
    receipt: BundleVersionIdentity
    artifacts: list[ManifestEntry]

    def canonical_payload(self) -> dict[str, Any]:
        """Return the exact dict that gets digested and stored, keyed by wire alias."""
        return self.model_dump(by_alias=True)


class DeployedMarker(_StrictModel):
    """The ``deployed/<environment>.json`` record of what an environment runs.

    Written after a healthy deploy (last-writer-wins) by
    ``src.modal.utils.record_deployment`` and read by artifact GC as its
    liveness signal — the ``manifest_digest`` here is what keeps a live
    artifact set from being collected. This is a persisted wire shape:
    keep the keys stable.
    """

    environment: str
    manifest_digest: str
    policyengine_version: str
    us_version: str
    uk_version: str
    us_data_version: str
    uk_data_version: str
    github_run_id: str
    github_run_url: str
    github_sha: str
    deployed_at: str


class DatasetBuildResult(_StrictModel):
    """Result of one dataset build; ``uploaded`` is False if the key existed."""

    year: int
    path: str
    uploaded: bool
    build_seconds: float
    size_bytes: int


class StateDatasetBuildResult(_StrictModel):
    """Result of one state's dataset build; ``uploaded`` is False if the key existed.

    ``build_seconds`` is how long the year's build had run when this state's
    file was uploaded, so the last result of a build gives its whole duration.
    """

    year: int
    state_code: str
    path: str
    uploaded: bool
    size_bytes: int
    build_seconds: float


class BaselineComputeResult(_StrictModel):
    """Outcome of one cohort baseline computation and its write-once upload.

    ``outcome`` is the baseline's artifact outcome (hit/incomplete/miss) from
    its ``ensure()`` call; seconds are wall-clock, rounded to 0.1.
    """

    year: int
    group: list[str]
    simulation_id: str
    outcome: ArtifactOutcome | None
    uploaded: bool
    compute_seconds: float
    size_bytes: int


class DeterminismVerdict(_StrictModel):
    """Whether a stored baseline matches a fresh recompute, column by column.

    ``differences`` lists at most the first 50 mismatches.
    """

    equal: bool
    differences: list[str]


class RemoteFunction(Protocol):
    """The slice of a Modal function handle the orchestration uses."""

    def remote(self, *args: Any) -> Any:  # noqa: ANN401  # mirrors modal.Function.remote; payloads and results vary per function
        """Run the function in its container and block until it returns."""
        ...

    def spawn(self, *args: Any) -> Any:  # noqa: ANN401  # mirrors modal.Function.spawn; payloads vary and the returned FunctionCall is untyped here
        """Start the call without waiting; the handle's ``get()`` returns the result."""
        ...
