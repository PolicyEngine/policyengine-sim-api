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

Plans are not stored, so their shape may change freely. The manifest is:
``ArtifactManifest.canonical_payload()`` is what gets digested and stored,
and the digest names the deployed file set, so any change to its shape
rotates published digests.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from policyengine_simulation_executor.stage12_cache.levels import DatasetLevel

# Mirrors baseline_artifacts.OUTCOME_*; the artifact outcome a worker
# observed when ensuring a baseline.
ArtifactOutcome = Literal["hit", "incomplete", "miss"]

MANIFEST_SCHEMA = "mf2"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BundleVersionIdentity(_StrictModel):
    """The version identity of the installed bundle a plan was made for.

    Travels under the wire key ``receipt``, but is deliberately NOT the
    install receipt file (``.policyengine-bundle-receipt.json``): that
    carries per-dataset content hashes, which live inside the artifact
    digests here, not in this version tuple. The image's download step
    refuses a manifest whose versions differ from the image's.
    """

    policyengine_version: str
    policyengine_core_version: str
    model_version: str
    data_version: str
    data_artifact_revision: str
    default_dataset: str


class YearFilePlanEntry(_StrictModel):
    """One year file: its level, identity, image folder and store presence.

    ``region`` and ``subset_code`` name a per-subset file's rows (``state/ca``,
    ``CA``); both are None for a whole dataset's year file.
    """

    country: str
    level: DatasetLevel
    year: int
    dataset: str
    region: str | None
    subset_code: str | None
    digest: str
    path: str
    filename: str
    folder: str
    exists: bool


class BaselinePlanEntry(_StrictModel):
    """One current-law baseline: the request it answers and the year file it reads.

    ``request`` is the synthetic request the runtime would receive.
    ``year_file_path`` and ``year_file_filename`` name the year file it runs on,
    which the builder downloads into ``folder`` when it is missing; the
    baseline lands beside it.
    """

    country: str
    level: DatasetLevel
    year: int
    region: str
    label: str
    request: dict[str, Any]
    year_file_path: str
    year_file_filename: str
    folder: str
    digest: str
    path: str
    simulation_id: str
    storage_id: str
    exists: bool


class SourceFilePlanEntry(_StrictModel):
    """A source dataset copied into the image as is, addressed by its certified hash."""

    country: str
    level: DatasetLevel
    dataset: str
    sha256: str
    path: str
    filename: str
    folder: str
    exists: bool


class PrecomputePlan(_StrictModel):
    """Everything the store should contain for one country's installed bundle."""

    country: str
    year_files: list[YearFilePlanEntry]
    baselines: list[BaselinePlanEntry]
    source_files: list[SourceFilePlanEntry] = Field(default_factory=list)
    receipt: BundleVersionIdentity


class WorkSelection(_StrictModel):
    """The subset of a plan that actually needs computing."""

    year_files: list[YearFilePlanEntry]
    baselines: list[BaselinePlanEntry]
    source_files: list[SourceFilePlanEntry] = Field(default_factory=list)


class ManifestEntry(_StrictModel):
    """One file the deploy image must fetch into its prebuilt folder.

    ``sha256`` and ``size_bytes`` are the stored object's, which the download
    step checks. ``year`` is None for a source file, and ``region`` for a
    whole dataset's year file.
    """

    kind: Literal["year_file", "baseline", "source_file"]
    level: DatasetLevel
    region: str | None
    path: str
    filename: str
    folder: str
    year: int | None
    digest: str
    sha256: str
    size_bytes: int


class ArtifactManifest(_StrictModel):
    """One country's published deploy manifest (content-addressed in the store)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # "schema" is the wire key; the Python name avoids shadowing
    # BaseModel's deprecated .schema attribute.
    manifest_schema: Literal["mf2"] = Field(alias="schema")
    country: str
    receipt: BundleVersionIdentity
    artifacts: list[ManifestEntry]

    def canonical_payload(self) -> dict[str, Any]:
        """Return the exact dict that gets digested and stored, keyed by wire alias."""
        return self.model_dump(by_alias=True, mode="json")


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


class YearFileBuildResult(_StrictModel):
    """Result of one year file's build; ``uploaded`` is False if the key existed.

    ``build_seconds`` is how long the build had run when this file was
    uploaded, so the last result of a build gives its whole duration.
    """

    year: int
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
    label: str
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
