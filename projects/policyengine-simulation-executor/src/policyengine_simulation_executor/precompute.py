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

Baselines are built through the executor's own request path
(``resolve_region`` + ``_build_simulation`` on each level handler's
synthetic request), so scoping, extra variables, and the deterministic
simulation id match a production run's by construction, not by convention —
and a regression test asserts the writer and reader derive identical
identities. Which years, files and baselines a country builds comes from its
level handlers (``stage12_cache.levels``). All
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
from typing import TYPE_CHECKING, Any

from policyengine_simulation_executor.precompute_baselines import (
    compute_baseline_impl,
    verify_determinism_impl,
)
from policyengine_simulation_executor.precompute_datasets import (
    build_year_files_impl,
)
from policyengine_simulation_executor.precompute_models import (
    MANIFEST_SCHEMA,
    ArtifactManifest,
    BaselineComputeResult,
    BaselinePlanEntry,
    DeterminismVerdict,
    ManifestEntry,
    PrecomputePlan,
    RemoteFunction,
    YearFileBuildResult,
    YearFilePlanEntry,
)
from policyengine_simulation_executor.precompute_planning import (
    baseline_target_identity,
    country_prebuilt,
    plan_artifacts_impl,
    select_work,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from policyengine_simulation_executor.artifact_store import StoredObject

__all__ = [
    "MANIFEST_DIGEST_PREFIX",
    "MANIFEST_SCHEMA",
    "PrecomputeFunctions",
    "baseline_target_identity",
    "build_manifest",
    "build_year_files_impl",
    "compute_baseline_impl",
    "country_prebuilt",
    "logger",
    "manifest_digest_line",
    "plan_artifacts_impl",
    "publish_manifest_impl",
    "run_precompute",
    "select_work",
    "verify_determinism_impl",
]

logger = logging.getLogger(__name__)

# The stdout contract the CI deploy job parses; keep format changes in
# lockstep with the workflow side (covered by a regression test).
MANIFEST_DIGEST_PREFIX = "MANIFEST_DIGEST="


def manifest_digest_line(digest: str) -> str:
    """Format the ``MANIFEST_DIGEST=<digest>`` line the CI deploy job parses."""
    return f"{MANIFEST_DIGEST_PREFIX}{digest}"


def build_manifest(
    plan: PrecomputePlan, stored: Mapping[str, StoredObject]
) -> ArtifactManifest:
    """Build a country's deploy manifest: the receipt and every file the image fetches.

    Year files, then baselines, then source files, each in plan order.
    ``stored`` maps each planned path to the SHA-256 and size the store holds
    for it, which the image's download step checks.
    """
    artifacts = [
        ManifestEntry(
            kind="year_file",
            level=entry.level,
            region=entry.region,
            path=entry.path,
            filename=entry.filename,
            folder=entry.folder,
            year=entry.year,
            digest=entry.digest,
            sha256=stored[entry.path].sha256,
            size_bytes=stored[entry.path].size_bytes,
        )
        for entry in plan.year_files
    ]
    artifacts += [
        ManifestEntry(
            kind="baseline",
            level=entry.level,
            region=entry.region,
            path=entry.path,
            filename=f"{entry.storage_id}.h5",
            folder=entry.folder,
            year=entry.year,
            digest=entry.digest,
            sha256=stored[entry.path].sha256,
            size_bytes=stored[entry.path].size_bytes,
        )
        for entry in plan.baselines
    ]
    artifacts += [
        ManifestEntry(
            kind="source_file",
            level=entry.level,
            region=None,
            path=entry.path,
            filename=entry.filename,
            folder=entry.folder,
            year=None,
            # A source file is addressed by its certified SHA-256.
            digest=entry.sha256,
            sha256=stored[entry.path].sha256,
            size_bytes=stored[entry.path].size_bytes,
        )
        for entry in plan.source_files
    ]
    return ArtifactManifest(
        # The field's alias, which Pydantic accepts (and which its type
        # checker support knows) at construction.
        schema=MANIFEST_SCHEMA,
        country=plan.country,
        receipt=plan.receipt,
        artifacts=artifacts,
    )


def publish_manifest_impl(bucket: str, plan: PrecomputePlan) -> str:
    """Store the plan's manifest write-once and return its content digest.

    Reads each planned object's SHA-256 and size from the store first, so a
    missing object fails the publish (``FileNotFoundError``) before anything
    is written.
    """
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )

    store = ArtifactStore(bucket)
    paths = [
        entry.path for entry in [*plan.year_files, *plan.baselines, *plan.source_files]
    ]
    stored = {path: store.describe(path) for path in paths}
    manifest = build_manifest(plan, stored)
    return store.write_manifest(manifest.canonical_payload())


@dataclass(frozen=True)
class PrecomputeFunctions:
    """The six Modal function handles the precompute orchestration calls."""

    plan_artifacts: RemoteFunction
    build_dataset: RemoteFunction
    build_state_datasets: RemoteFunction
    compute_baseline: RemoteFunction
    verify_determinism: RemoteFunction
    publish_manifest: RemoteFunction


@dataclass(frozen=True)
class _SubsetBuild:
    """One running build of a year's subset year files of one dataset."""

    label: str
    paths: frozenset[str]
    handle: Any


def _wait_for_subset_builds(
    builds: list[_SubsetBuild], echo: Callable[[str], None]
) -> None:
    for build in builds:
        results = [
            YearFileBuildResult.model_validate(item) for item in build.handle.get()
        ]
        uploaded = [result for result in results if result.uploaded]
        echo(
            f"{build.label}: {len(results)} built, {len(uploaded)} uploaded "
            f"({sum(result.size_bytes for result in uploaded)} bytes) in "
            f"{max((result.build_seconds for result in results), default=0.0)} s"
        )


def _start_year_file_builds(
    bucket: str, entries: list[YearFilePlanEntry], functions: PrecomputeFunctions
) -> tuple[list[tuple[YearFilePlanEntry, Any]], list[_SubsetBuild]]:
    """Start a build per whole-dataset year file and per year and subset dataset."""
    whole = [
        (entry, functions.build_dataset.spawn(bucket, entry.model_dump()))
        for entry in entries
        if entry.subset_code is None
    ]
    grouped: dict[tuple[int, str], list[YearFilePlanEntry]] = {}
    for entry in entries:
        if entry.subset_code is not None:
            grouped.setdefault((entry.year, entry.dataset), []).append(entry)
    subsets = [
        _SubsetBuild(
            label=f"Year files {year} {dataset}",
            paths=frozenset(entry.path for entry in group),
            handle=functions.build_state_datasets.spawn(
                bucket, year, [entry.model_dump() for entry in group]
            ),
        )
        for (year, dataset), group in grouped.items()
    ]
    return whole, subsets


def _check_determinism(
    bucket: str,
    computed: list[BaselinePlanEntry],
    functions: PrecomputeFunctions,
    echo: Callable[[str], None],
) -> None:
    """Recompute the first computed baseline of each level; stop on any difference."""
    probes: dict[str, BaselinePlanEntry] = {}
    for entry in computed:
        probes.setdefault(entry.level, entry)
    for probe in probes.values():
        echo(f"Determinism check: recomputing {probe.year} {probe.label}")
        verdict = DeterminismVerdict.model_validate(
            functions.verify_determinism.remote(bucket, probe.model_dump())
        )
        if not verdict.equal:
            raise SystemExit(
                "Determinism check FAILED: " + "; ".join(verdict.differences)
            )
        echo("Determinism check passed")


def run_precompute(
    bucket: str,
    *,
    country: str = "us",
    force: bool,
    functions: PrecomputeFunctions,
    echo: Callable[[str], None] = print,
) -> str:
    """Orchestrate one country's precompute: plan, build misses, check, publish.

    Takes the six Modal function handles as ``functions`` (each offering
    ``.remote``/``.spawn``) so the flow is testable with plain fakes. A
    whole dataset's year file is one ``build_dataset`` call; subset year
    files are one ``build_state_datasets`` call per year and dataset. Each
    baseline starts once the build of the year file it reads has finished,
    when this run builds that file, and publishing waits for every build.
    Everything crossing a handle is a plain dict (Modal's serialization
    boundary); it is validated back into models immediately on return.
    The determinism check recomputes the first computed baseline of each
    level that computed any (a no-op run stays a no-op). Returns the manifest
    digest; the LAST echoed line is the ``MANIFEST_DIGEST=`` contract the
    deploy job parses.
    """
    echo(f"Planning {country} against gs://{bucket} (force={force})")
    plan = PrecomputePlan.model_validate(
        functions.plan_artifacts.remote(bucket, country)
    )
    work = select_work(plan, force=force)
    echo(
        f"Plan: {len(plan.year_files)} year files ({len(work.year_files)} to "
        f"build), {len(plan.baselines)} baselines ({len(work.baselines)} to compute)"
    )

    whole_builds, subset_builds = _start_year_file_builds(
        bucket, work.year_files, functions
    )
    for entry, handle in whole_builds:
        result = YearFileBuildResult.model_validate(handle.get())
        echo(
            f"year file {entry.dataset} {result.year} built in "
            f"{result.build_seconds}s ({result.size_bytes} bytes, "
            f"uploaded={result.uploaded})"
        )

    subset_paths = {path for build in subset_builds for path in build.paths}
    on_subset_files = [
        entry for entry in work.baselines if entry.year_file_path in subset_paths
    ]
    baseline_handles = [
        functions.compute_baseline.spawn(bucket, entry.model_dump())
        for entry in work.baselines
        if entry.year_file_path not in subset_paths
    ]
    if on_subset_files:
        _wait_for_subset_builds(subset_builds, echo)
        subset_builds = []
        baseline_handles += [
            functions.compute_baseline.spawn(bucket, entry.model_dump())
            for entry in on_subset_files
        ]
    for handle in baseline_handles:
        result = BaselineComputeResult.model_validate(handle.get())
        echo(
            f"baseline year={result.year} {result.label} id={result.simulation_id} "
            f"outcome={result.outcome} in {result.compute_seconds}s "
            f"(uploaded={result.uploaded})"
        )

    _check_determinism(bucket, work.baselines, functions, echo)
    _wait_for_subset_builds(subset_builds, echo)

    manifest_digest = functions.publish_manifest.remote(bucket, plan.model_dump())
    echo(manifest_digest_line(manifest_digest))
    return manifest_digest
