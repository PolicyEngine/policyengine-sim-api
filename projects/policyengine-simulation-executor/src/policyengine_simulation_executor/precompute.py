"""Library half of the artifact precompute (the write path of the pipeline).

``src/modal/precompute_app.py`` holds only the Modal plumbing — app, image,
function declarations, local entrypoint — and every function body lives
here, mirroring the deployed app's split (``run_simulation`` ->
``simulation_runtime.run_simulation_impl``). Keeping the logic in the
executor package makes it testable without Modal fakes and importable by
the CI deploy job and the GC planner.

Data flow is strictly typed via ``precompute_models``: plans, entries,
version identities, manifests, and worker results are Pydantic models
everywhere inside the library; plain dicts appear only at the Modal
serialization boundary (the app wrappers validate structured inputs on
the way in and dump model results on the way out; bare strings — the
bucket, the manifest digest — cross as-is).

The store is content-addressed, so a precompute run is idempotent: it
plans against the store and computes only misses; a re-run against a warm
store is a no-op. ``force`` recomputes everything but CANNOT replace
existing store objects (uploads are write-once) — healing a bad artifact
means deleting its object first, then re-running.

Cohort baselines are built through the executor's own request path
(``resolve_region`` + ``_build_simulation`` on a synthetic child request),
so scoping, extra variables, and the deterministic simulation id match the
production child's by construction, not by convention — and a regression
test asserts the writer and reader derive identical identities. All
identity collection and store I/O must run inside containers: the bundle
receipt that the keys digest lives in the image, not on the deploying
machine.

The pipeline's steps live in precompute_planning, precompute_datasets and
precompute_baselines. This module runs them, builds and publishes the
manifest, and re-exports their public names.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from policyengine_simulation_executor.precompute_baselines import (
    compute_baseline_impl,
    verify_determinism_impl,
)
from policyengine_simulation_executor.precompute_datasets import (
    build_dataset_impl,
    build_state_datasets_impl,
)
from policyengine_simulation_executor.precompute_models import (
    ArtifactManifest,
    BaselineComputeResult,
    DatasetBuildResult,
    DeterminismVerdict,
    ManifestArtifact,
    ManifestEntry,
    PrecomputePlan,
    RemoteFunction,
    StateDatasetArtifact,
    StateDatasetBuildResult,
)
from policyengine_simulation_executor.precompute_planning import (
    PRECOMPUTE_COUNTRY,
    PRECOMPUTE_YEARS,
    StateDatasetTarget,
    cohort_identity,
    cohort_params,
    plan_artifacts_impl,
    select_work,
    state_dataset_targets,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "MANIFEST_DIGEST_PREFIX",
    "MANIFEST_SCHEMA",
    "PRECOMPUTE_COUNTRY",
    "PRECOMPUTE_YEARS",
    "PrecomputeFunctions",
    "StateDatasetTarget",
    "build_dataset_impl",
    "build_manifest",
    "build_state_datasets_impl",
    "cohort_identity",
    "cohort_params",
    "compute_baseline_impl",
    "logger",
    "manifest_digest_line",
    "plan_artifacts_impl",
    "publish_manifest_impl",
    "run_precompute",
    "select_work",
    "state_dataset_targets",
    "verify_determinism_impl",
]

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA = "mf1"

# The stdout contract the CI deploy job parses; keep format changes in
# lockstep with the workflow side (covered by a regression test).
MANIFEST_DIGEST_PREFIX = "MANIFEST_DIGEST="


def manifest_digest_line(digest: str) -> str:
    """Format the ``MANIFEST_DIGEST=<digest>`` line the CI deploy job parses."""
    return f"{MANIFEST_DIGEST_PREFIX}{digest}"


def build_manifest(plan: PrecomputePlan) -> ArtifactManifest:
    """Build the deploy manifest: the receipt and each artifact the image must fetch.

    Datasets, then baselines, then per-state datasets, each in plan order, so
    a plan without per-state datasets keeps the manifest it always had.
    """
    artifacts: list[ManifestEntry] = [
        ManifestArtifact(
            type="dataset",
            path=entry.path,
            filename=entry.filename,
            year=entry.year,
            digest=entry.digest,
        )
        for entry in plan.datasets
    ]
    artifacts += [
        ManifestArtifact(
            type="baseline",
            path=entry.path,
            filename=entry.path.rsplit("/", maxsplit=1)[-1],
            year=entry.year,
            digest=entry.digest,
        )
        for entry in plan.baselines
    ]
    artifacts += [
        StateDatasetArtifact(
            type="state_dataset",
            path=entry.path,
            filename=entry.filename,
            folder=entry.folder,
            state_code=entry.state_code,
            year=entry.year,
            digest=entry.digest,
        )
        for entry in plan.state_datasets
    ]
    return ArtifactManifest(
        # The field's alias, which Pydantic accepts (and which its type
        # checker support knows) at construction.
        schema=MANIFEST_SCHEMA,
        country=PRECOMPUTE_COUNTRY,
        receipt=plan.receipt,
        artifacts=artifacts,
    )


def publish_manifest_impl(bucket: str, plan: PrecomputePlan) -> str:
    """Store the plan's manifest write-once and return its content digest."""
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )

    manifest = build_manifest(plan)
    return ArtifactStore(bucket).write_manifest(manifest.canonical_payload())


@dataclass(frozen=True)
class PrecomputeFunctions:
    """The six Modal function handles the precompute orchestration calls."""

    plan_artifacts: RemoteFunction
    build_dataset: RemoteFunction
    build_state_datasets: RemoteFunction
    compute_baseline: RemoteFunction
    verify_determinism: RemoteFunction
    publish_manifest: RemoteFunction


def run_precompute(
    bucket: str,
    *,
    force: bool,
    functions: PrecomputeFunctions,
    echo: Callable[[str], None] = print,
) -> str:
    """Orchestrate the local entrypoint: plan, spawn misses, gate, publish.

    Takes the six Modal function handles as ``functions`` (each offering
    ``.remote``/``.spawn``) so the flow is testable with plain fakes.
    Per-state dataset builds (one container per year and dataset) start with
    the dataset builds and are waited for only before the manifest is
    published, so the baselines never wait for them.
    Everything crossing a handle is a plain dict (Modal's serialization
    boundary); it is validated back into models immediately on return.
    The determinism gate runs only when baselines were actually computed
    (a no-op run must stay a no-op). Returns the manifest digest; the
    LAST echoed line is the ``MANIFEST_DIGEST=`` contract the deploy job
    parses.
    """
    echo(f"Planning against gs://{bucket} (force={force})")
    plan = PrecomputePlan.model_validate(functions.plan_artifacts.remote(bucket))
    work = select_work(plan, force=force)
    echo(
        f"Plan: {len(plan.datasets)} datasets ({len(work.datasets)} to "
        f"build), {len(plan.state_datasets)} state datasets "
        f"({len(work.state_datasets)} to build), {len(plan.baselines)} baselines "
        f"({len(work.baselines)} to compute)"
    )

    dataset_handles = [
        functions.build_dataset.spawn(bucket, entry.model_dump())
        for entry in work.datasets
    ]
    # One build prepares one dataset's year file, so builds are per year and
    # dataset.
    state_builds: dict[tuple[int, str], list[dict]] = {}
    for entry in work.state_datasets:
        state_builds.setdefault((entry.year, entry.dataset), []).append(
            entry.model_dump()
        )
    state_handles = [
        (year, dataset, functions.build_state_datasets.spawn(bucket, year, entries))
        for (year, dataset), entries in state_builds.items()
    ]
    for handle in dataset_handles:
        result = DatasetBuildResult.model_validate(handle.get())
        echo(
            f"dataset year={result.year} built in "
            f"{result.build_seconds}s ({result.size_bytes} bytes, "
            f"uploaded={result.uploaded})"
        )

    baseline_handles = [
        functions.compute_baseline.spawn(bucket, entry.model_dump())
        for entry in work.baselines
    ]
    for handle in baseline_handles:
        result = BaselineComputeResult.model_validate(handle.get())
        echo(
            f"baseline year={result.year} id={result.simulation_id} "
            f"outcome={result.outcome} in {result.compute_seconds}s "
            f"(uploaded={result.uploaded})"
        )

    if work.baselines:
        probe = work.baselines[0]
        echo(f"Determinism gate: recomputing year={probe.year} group={probe.group}")
        verdict = DeterminismVerdict.model_validate(
            functions.verify_determinism.remote(bucket, probe.model_dump())
        )
        if not verdict.equal:
            raise SystemExit(
                "Determinism gate FAILED: " + "; ".join(verdict.differences)
            )
        echo("Determinism gate passed")

    for year, dataset, handle in state_handles:
        results = [
            StateDatasetBuildResult.model_validate(result) for result in handle.get()
        ]
        uploaded = [result for result in results if result.uploaded]
        echo(
            f"State datasets {year} {dataset}: {len(results)} built, "
            f"{len(uploaded)} uploaded "
            f"({sum(result.size_bytes for result in uploaded)} bytes) in "
            f"{max((result.build_seconds for result in results), default=0.0)} s"
        )

    manifest_digest = functions.publish_manifest.remote(bucket, plan.model_dump())
    echo(manifest_digest_line(manifest_digest))
    return manifest_digest
