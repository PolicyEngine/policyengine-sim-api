"""Build and publish the independent Stage 12 US cache.

The steps live in precompute_planning, precompute_datasets and
precompute_baselines. This module runs them, builds and publishes the
manifest, and re-exports their public names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .models import (
    BaselineBuildResult,
    CacheManifest,
    CacheManifestEntry,
    DatasetBuildResult,
    DeterminismVerdict,
    ManifestArtifact,
    PrecomputePlan,
    StateDatasetBuildResult,
    StateDatasetManifestArtifact,
)
from .precompute_baselines import compute_baseline_impl, verify_determinism_impl
from .precompute_datasets import build_dataset_impl, build_state_datasets_impl
from .precompute_planning import (
    DETERMINISM_SCHEMA,
    PRECOMPUTE_YEARS,
    StateDatasetTarget,
    determinism_record,
    determinism_record_path,
    plan_artifacts_impl,
    select_work,
    state_dataset_targets,
)
from .store import Stage12CacheStore

if TYPE_CHECKING:
    from collections.abc import Callable

    import modal

__all__ = [
    "DETERMINISM_SCHEMA",
    "MANIFEST_DIGEST_PREFIX",
    "PRECOMPUTE_YEARS",
    "CachePrecomputeFunctions",
    "StateDatasetTarget",
    "build_dataset_impl",
    "build_manifest",
    "build_state_datasets_impl",
    "compute_baseline_impl",
    "determinism_record",
    "determinism_record_path",
    "manifest_digest_line",
    "plan_artifacts_impl",
    "publish_manifest_impl",
    "run_precompute",
    "select_work",
    "state_dataset_targets",
    "verify_determinism_impl",
]

MANIFEST_DIGEST_PREFIX = "STAGE12_CACHE_MANIFEST_DIGEST="


def manifest_digest_line(digest: str) -> str:
    """Return the output line that reports the published manifest digest."""
    return f"{MANIFEST_DIGEST_PREFIX}{digest}"


def build_manifest(plan: PrecomputePlan, store: Stage12CacheStore) -> CacheManifest:
    """List every planned file with the digest and size the bucket reports.

    Datasets, then baselines, then state datasets, each in plan order, so a
    plan without state datasets keeps the manifest it always had.
    """
    artifacts: list[CacheManifestEntry] = []
    for entry in plan.datasets:
        stored = store.describe(entry.path)
        artifacts.append(
            ManifestArtifact(
                type="dataset",
                path=entry.path,
                filename=entry.filename,
                year=entry.year,
                identity_digest=entry.digest,
                content_sha256=stored.content_sha256,
                size_bytes=stored.size_bytes,
            )
        )
    for entry in plan.baselines:
        stored = store.describe(entry.path)
        artifacts.append(
            ManifestArtifact(
                type="baseline",
                path=entry.path,
                filename=entry.filename,
                year=entry.year,
                identity_digest=entry.digest,
                content_sha256=stored.content_sha256,
                size_bytes=stored.size_bytes,
            )
        )
    for entry in plan.state_datasets:
        stored = store.describe(entry.path)
        artifacts.append(
            StateDatasetManifestArtifact(
                type="state_dataset",
                path=entry.path,
                filename=entry.filename,
                folder=entry.folder,
                state_code=entry.state_code,
                year=entry.year,
                identity_digest=entry.digest,
                content_sha256=stored.content_sha256,
                size_bytes=stored.size_bytes,
            )
        )
    return CacheManifest(
        years=plan.years,
        partition_sha256=plan.partition_sha256,
        bundle=plan.bundle,
        artifacts=tuple(artifacts),
    )


def publish_manifest_impl(bucket: str, plan: PrecomputePlan) -> str:
    """Store the cache manifest and return its digest.

    Raises ``RuntimeError`` when the bucket holds no passing determinism
    record for the planned baselines.
    """
    store = Stage12CacheStore(bucket)
    # Checked here, not only by the caller, so no run can publish baselines
    # that never passed the determinism check.
    if not store.exists(determinism_record_path(plan.baselines)):
        msg = "Stage 12 cache baselines have not passed determinism"
        raise RuntimeError(msg)
    return store.write_manifest(build_manifest(plan, store).canonical_payload())


@dataclass(frozen=True)
class CachePrecomputeFunctions:
    """The six Modal function handles the cache precompute orchestration calls."""

    plan_artifacts: modal.Function
    build_dataset: modal.Function
    build_state_datasets: modal.Function
    compute_baseline: modal.Function
    verify_determinism: modal.Function
    publish_manifest: modal.Function


def run_precompute(
    bucket: str,
    *,
    functions: CachePrecomputeFunctions,
    echo: Callable[[str], None] = print,
) -> str:
    """Build the missing cache files, check determinism, publish the manifest.

    Missing datasets are built first, then missing baselines, in parallel
    Modal calls. Missing state datasets are built alongside both, one call per
    year and dataset, and waited for only before the manifest is published, so
    the baselines never wait for them. Prints the manifest digest through
    ``echo`` and returns it. Exits with ``SystemExit`` when the determinism
    check fails.
    """
    plan = PrecomputePlan.model_validate(functions.plan_artifacts.remote(bucket))
    work = select_work(plan)
    dataset_calls = [
        functions.build_dataset.spawn(bucket, entry.model_dump())
        for entry in work.datasets
    ]
    # One build prepares one dataset's year file, so builds are per year and
    # dataset.
    state_builds: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for entry in work.state_datasets:
        state_builds.setdefault((entry.year, entry.dataset), []).append(
            entry.model_dump()
        )
    state_calls = [
        (year, dataset, functions.build_state_datasets.spawn(bucket, year, entries))
        for (year, dataset), entries in state_builds.items()
    ]
    for call in dataset_calls:
        DatasetBuildResult.model_validate(call.get())
    baseline_calls = [
        functions.compute_baseline.spawn(bucket, entry.model_dump())
        for entry in work.baselines
    ]
    for call in baseline_calls:
        BaselineBuildResult.model_validate(call.get())
    # A run that computed baselines checks one of them. A run that computed
    # none still checks one when no earlier run recorded a pass, so a failed
    # check cannot be skipped by running again.
    if work.baselines or not plan.determinism_verified:
        probe = work.baselines[0] if work.baselines else plan.baselines[0]
        verdict = DeterminismVerdict.model_validate(
            functions.verify_determinism.remote(
                bucket, plan.model_dump(), probe.model_dump()
            )
        )
        if not verdict.equal:
            raise SystemExit(
                "Stage 12 cache determinism validation failed: "
                + "; ".join(verdict.differences)
            )
    for year, dataset, call in state_calls:
        results = [
            StateDatasetBuildResult.model_validate(result) for result in call.get()
        ]
        uploaded = [result for result in results if result.uploaded]
        echo(
            f"State datasets {year} {dataset}: {len(results)} built, "
            f"{len(uploaded)} uploaded "
            f"({sum(result.size_bytes for result in uploaded)} bytes) in "
            f"{max((result.build_seconds for result in results), default=0.0)} s"
        )
    digest = functions.publish_manifest.remote(bucket, plan.model_dump())
    if not isinstance(digest, str) or not digest:
        msg = "Stage 12 cache manifest publisher returned no digest"
        raise RuntimeError(msg)
    echo(manifest_digest_line(digest))
    return digest
