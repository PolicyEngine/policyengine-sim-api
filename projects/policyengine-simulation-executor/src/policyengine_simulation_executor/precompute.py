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
(``_resolve_region`` + ``_build_simulation`` on a synthetic child request),
so scoping, extra variables, and the deterministic simulation id match the
production child's by construction, not by convention — and a regression
test asserts the writer and reader derive identical identities. All
identity collection and store I/O must run inside containers: the bundle
receipt that the keys digest lives in the image, not on the deploying
machine.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from policyengine_simulation_executor.artifact_keys import (
    BaselineArtifactIdentity,
    baseline_artifact_filename,
)
from policyengine_simulation_executor.precompute_models import (
    ArtifactManifest,
    BaselineComputeResult,
    BaselinePlanEntry,
    BundleVersionIdentity,
    DatasetBuildResult,
    DatasetPlanEntry,
    DeterminismVerdict,
    ManifestArtifact,
    PrecomputePlan,
    RemoteFunction,
    WorkSelection,
)

logger = logging.getLogger(__name__)

PRECOMPUTE_COUNTRY = "us"
# The user-facing priority order (2026 first); with parallel waves it only
# orders spawn submission, but keep it intentional.
PRECOMPUTE_YEARS = [2026, 2027, 2025]

MANIFEST_SCHEMA = "mf1"

# The stdout contract the CI deploy job parses; keep format changes in
# lockstep with the workflow side (covered by a regression test).
MANIFEST_DIGEST_PREFIX = "MANIFEST_DIGEST="


def manifest_digest_line(digest: str) -> str:
    return f"{MANIFEST_DIGEST_PREFIX}{digest}"


def cohort_params(year: int, group: list[str]) -> dict:
    """The synthetic request a segmented-national child would receive."""
    return {
        "country": PRECOMPUTE_COUNTRY,
        "scope": "macro",
        "region_group": list(group),
        "time_period": year,
    }


def _cohort_resolution(year: int, group: list[str]):
    """Cohort request params, country module, and region resolution.

    The planner's identity (``cohort_identity``) and the worker's
    simulation (``_prepare_cohort_baseline``) must derive from ONE
    resolution or the in-container id checks abort the run; sharing this
    prologue removes the drift surface.
    """
    from policyengine_simulation_executor.simulation_runtime import (
        _country_module,
        _resolve_region,
    )

    params = cohort_params(year, group)
    country_module = _country_module(PRECOMPUTE_COUNTRY)
    resolution = _resolve_region(
        country_module=country_module,
        country=PRECOMPUTE_COUNTRY,
        params=params,
    )
    return params, country_module, resolution


def cohort_identity(year: int, group: list[str]) -> BaselineArtifactIdentity:
    """Identity via the executor's own resolution path (writer==reader)."""
    from policyengine_simulation_executor.baseline_artifacts import (
        qualifying_baseline_identity,
    )

    params, _, resolution = _cohort_resolution(year, group)
    identity = qualifying_baseline_identity(
        params,
        country=PRECOMPUTE_COUNTRY,
        policy=None,
        region_code=resolution.code,
        scoping_strategy=resolution.scoping_strategy,
        year=year,
    )
    if identity is None:
        raise RuntimeError(
            f"Cohort request for year {year} group {group} does not qualify "
            "for a deterministic baseline id — writer and reader predicates "
            "have diverged."
        )
    return identity


def select_work(plan: PrecomputePlan, *, force: bool) -> WorkSelection:
    """Which plan entries need computing."""
    return WorkSelection(
        datasets=[entry for entry in plan.datasets if force or not entry.exists],
        baselines=[entry for entry in plan.baselines if force or not entry.exists],
    )


def build_manifest(plan: PrecomputePlan) -> ArtifactManifest:
    """The deploy manifest: receipt + every artifact the image must fetch."""
    artifacts = [
        ManifestArtifact(
            type="dataset",
            path=entry.path,
            filename=entry.filename,
            year=entry.year,
            digest=entry.digest,
        )
        for entry in plan.datasets
    ] + [
        ManifestArtifact(
            type="baseline",
            path=entry.path,
            filename=baseline_artifact_filename(entry.simulation_id),
            year=entry.year,
            digest=entry.digest,
        )
        for entry in plan.baselines
    ]
    return ArtifactManifest(
        manifest_schema=MANIFEST_SCHEMA,
        country=PRECOMPUTE_COUNTRY,
        receipt=plan.receipt,
        artifacts=artifacts,
    )


def plan_artifacts_impl(bucket: str) -> PrecomputePlan:
    """Compute every expected artifact identity and its store presence."""
    from policyengine_simulation_executor.artifact_keys import (
        collect_dataset_identity,
    )
    from policyengine_simulation_executor.artifact_store import ArtifactStore
    from policyengine_simulation_executor.national_partition import (
        national_region_groups,
    )
    from policyengine_simulation_executor.release_bundle import (
        get_country_release_bundle,
    )

    store = ArtifactStore(bucket)
    groups = national_region_groups(PRECOMPUTE_COUNTRY)
    if not groups:
        raise RuntimeError(f"No national partition for {PRECOMPUTE_COUNTRY!r}")

    datasets: list[DatasetPlanEntry] = []
    baselines: list[BaselinePlanEntry] = []
    for year in PRECOMPUTE_YEARS:
        dataset_identity = collect_dataset_identity(PRECOMPUTE_COUNTRY, year)
        datasets.append(
            DatasetPlanEntry(
                year=year,
                digest=dataset_identity.digest,
                path=dataset_identity.store_path,
                filename=dataset_identity.filename,
                exists=store.exists(dataset_identity.store_path),
            )
        )
        for group in groups:
            identity = cohort_identity(year, group)
            baselines.append(
                BaselinePlanEntry(
                    year=year,
                    group=list(group),
                    region=identity.region,
                    digest=identity.digest,
                    path=identity.store_path,
                    simulation_id=identity.simulation_id,
                    exists=store.exists(identity.store_path),
                )
            )

    bundle = get_country_release_bundle(PRECOMPUTE_COUNTRY)
    receipt = BundleVersionIdentity(
        policyengine_version=bundle.policyengine_version,
        model_version=bundle.model_version,
        data_version=bundle.data_version,
        data_artifact_revision=bundle.data_artifact_revision,
        default_dataset=bundle.default_dataset,
    )
    return PrecomputePlan(datasets=datasets, baselines=baselines, receipt=receipt)


def build_dataset_impl(bucket: str, expected: DatasetPlanEntry) -> DatasetBuildResult:
    """Wave 1: build one single-year dataset and upload it."""
    from pathlib import Path

    from policyengine_simulation_executor.artifact_keys import (
        collect_dataset_identity,
    )
    from policyengine_simulation_executor.artifact_store import ArtifactStore
    from policyengine_simulation_executor.release_bundle import (
        get_country_release_bundle,
    )
    from policyengine_simulation_executor.simulation_runtime import (
        _country_module,
        resolve_data_folder,
    )

    identity = collect_dataset_identity(PRECOMPUTE_COUNTRY, expected.year)
    if identity.store_path != expected.path:
        raise RuntimeError(
            "Planned and in-container dataset identities disagree "
            f"({expected.path} != {identity.store_path}); refusing to upload "
            "under a mismatched key."
        )

    data_folder = resolve_data_folder()
    country_module = _country_module(PRECOMPUTE_COUNTRY)
    started = time.monotonic()
    country_module.ensure_datasets(
        datasets=[get_country_release_bundle(PRECOMPUTE_COUNTRY).default_dataset],
        years=[expected.year],
        data_folder=data_folder,
    )
    build_seconds = time.monotonic() - started

    local_file = Path(data_folder) / identity.filename
    if not local_file.exists():
        raise RuntimeError(f"ensure_datasets produced no file at {local_file}")
    uploaded = ArtifactStore(bucket).upload_file(identity.store_path, local_file)
    return DatasetBuildResult(
        year=expected.year,
        path=identity.store_path,
        uploaded=uploaded,
        build_seconds=round(build_seconds, 1),
        size_bytes=local_file.stat().st_size,
    )


def _prepare_cohort_baseline(bucket: str, expected: BaselinePlanEntry):
    """Shared by compute and verify: dataset download + simulation build."""
    from pathlib import Path

    from policyengine_simulation_executor.artifact_keys import (
        collect_dataset_identity,
    )
    from policyengine_simulation_executor.artifact_store import ArtifactStore
    from policyengine_simulation_executor.simulation_runtime import (
        _build_simulation,
        _load_dataset,
        resolve_data_folder,
    )

    store = ArtifactStore(bucket)
    data_folder = Path(resolve_data_folder())

    dataset_identity = collect_dataset_identity(PRECOMPUTE_COUNTRY, expected.year)
    local_dataset = data_folder / dataset_identity.filename
    if not local_dataset.exists():
        store.download_file(dataset_identity.store_path, local_dataset)

    params, country_module, resolution = _cohort_resolution(
        expected.year, expected.group
    )
    dataset = _load_dataset(
        params, country_module=country_module, region_resolution=resolution
    )
    baseline = _build_simulation(
        params,
        dataset=dataset,
        policy=None,
        scoping_strategy=resolution.scoping_strategy,
        region_code=resolution.code,
    )
    if baseline.id != expected.simulation_id:
        raise RuntimeError(
            "Planned and in-container baseline ids disagree "
            f"({expected.simulation_id} != {baseline.id}); refusing to act "
            "under a mismatched key."
        )

    # The extras economic_impact_analysis applies unconditionally before
    # ensure(); the artifact must carry them or every request would fail
    # the column guard and recompute.
    from policyengine.tax_benefit_models.us.analysis import (
        configure_budgetary_impact_variables,
    )

    configure_budgetary_impact_variables(baseline, baseline)
    return store, data_folder, baseline


def compute_baseline_impl(
    bucket: str, expected: BaselinePlanEntry
) -> BaselineComputeResult:
    """Wave 2: compute one cohort baseline and upload its output."""
    from policyengine_simulation_executor.baseline_artifacts import (
        ArtifactBaselineSimulation,
    )

    store, data_folder, baseline = _prepare_cohort_baseline(bucket, expected)
    if not isinstance(baseline, ArtifactBaselineSimulation):
        raise RuntimeError(
            "Precompute built a plain Simulation — the qualifying predicate "
            "rejected the cohort request shape."
        )

    started = time.monotonic()
    baseline.ensure()
    compute_seconds = time.monotonic() - started

    artifact_file = data_folder / baseline_artifact_filename(baseline.id)
    if not artifact_file.exists():
        raise RuntimeError(f"ensure() left no artifact at {artifact_file}")
    uploaded = store.upload_file(expected.path, artifact_file)
    return BaselineComputeResult(
        year=expected.year,
        group=list(expected.group),
        simulation_id=baseline.id,
        outcome=baseline.artifact_outcome,
        uploaded=uploaded,
        compute_seconds=round(compute_seconds, 1),
        size_bytes=artifact_file.stat().st_size,
    )


def verify_determinism_impl(
    bucket: str, expected: BaselinePlanEntry
) -> DeterminismVerdict:
    """Load the uploaded artifact, recompute independently, compare frames.

    Exact comparison of every entity column (values and dtypes) between the
    store artifact (downloaded and loaded through ``ensure()``, so the save/
    load round-trip is under test too) and a fresh independent run — the
    direct check of "same key, same bytes". File-level byte equality is
    deliberately not used (HDF5 container metadata is not guaranteed
    stable).
    """
    from policyengine.core import Simulation

    from policyengine_simulation_executor.baseline_artifacts import OUTCOME_HIT

    store, data_folder, baseline = _prepare_cohort_baseline(bucket, expected)
    artifact_file = data_folder / baseline_artifact_filename(baseline.id)
    if not artifact_file.exists():
        store.download_file(expected.path, artifact_file)
    baseline.ensure()
    if baseline.artifact_outcome != OUTCOME_HIT:
        raise RuntimeError(
            "Determinism gate could not load the artifact it verifies "
            f"(outcome={baseline.artifact_outcome})"
        )

    fresh = Simulation(
        dataset=baseline.dataset,
        tax_benefit_model_version=baseline.tax_benefit_model_version,
        policy=None,
        scoping_strategy=baseline.scoping_strategy,
        extra_variables=dict(baseline.extra_variables),
    )
    fresh.run()

    differences = []
    loaded = baseline.output_dataset.data.entity_data
    recomputed = fresh.output_dataset.data.entity_data
    for entity in sorted(set(loaded) | set(recomputed)):
        left, right = loaded.get(entity), recomputed.get(entity)
        if left is None or right is None:
            differences.append(f"{entity}: missing on one side")
            continue
        left_cols, right_cols = set(left.columns), set(right.columns)
        for column in sorted(left_cols ^ right_cols):
            differences.append(f"{entity}.{column}: only on one side")
        for column in sorted(left_cols & right_cols):
            if str(left[column].dtype) != str(right[column].dtype):
                differences.append(f"{entity}.{column}: dtype differs")
            elif not left[column].equals(right[column]):
                differences.append(f"{entity}.{column}: values differ")
    return DeterminismVerdict(equal=not differences, differences=differences[:50])


def publish_manifest_impl(bucket: str, plan: PrecomputePlan) -> str:
    from policyengine_simulation_executor.artifact_store import ArtifactStore

    manifest = build_manifest(plan)
    return ArtifactStore(bucket).write_manifest(manifest.canonical_payload())


def run_precompute(
    bucket: str,
    *,
    force: bool,
    plan_artifacts: RemoteFunction,
    build_dataset: RemoteFunction,
    compute_baseline: RemoteFunction,
    verify_determinism: RemoteFunction,
    publish_manifest: RemoteFunction,
    echo: Callable[[str], None] = print,
) -> str:
    """The local-entrypoint orchestration: plan, spawn misses, gate, publish.

    Takes the five Modal function handles as parameters (each offering
    ``.remote``/``.spawn``) so the flow is testable with plain fakes.
    Everything crossing a handle is a plain dict (Modal's serialization
    boundary); it is validated back into models immediately on return.
    The determinism gate runs only when baselines were actually computed
    (a no-op run must stay a no-op). Returns the manifest digest; the
    LAST echoed line is the ``MANIFEST_DIGEST=`` contract the deploy job
    parses.
    """
    echo(f"Planning against gs://{bucket} (force={force})")
    plan = PrecomputePlan.model_validate(plan_artifacts.remote(bucket))
    work = select_work(plan, force=force)
    echo(
        f"Plan: {len(plan.datasets)} datasets ({len(work.datasets)} to "
        f"build), {len(plan.baselines)} baselines "
        f"({len(work.baselines)} to compute)"
    )

    dataset_handles = [
        build_dataset.spawn(bucket, entry.model_dump()) for entry in work.datasets
    ]
    for handle in dataset_handles:
        result = DatasetBuildResult.model_validate(handle.get())
        echo(
            f"dataset year={result.year} built in "
            f"{result.build_seconds}s ({result.size_bytes} bytes, "
            f"uploaded={result.uploaded})"
        )

    baseline_handles = [
        compute_baseline.spawn(bucket, entry.model_dump()) for entry in work.baselines
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
            verify_determinism.remote(bucket, probe.model_dump())
        )
        if not verdict.equal:
            raise SystemExit(
                "Determinism gate FAILED: " + "; ".join(verdict.differences)
            )
        echo("Determinism gate passed")

    manifest_digest = publish_manifest.remote(bucket, plan.model_dump())
    echo(manifest_digest_line(manifest_digest))
    return manifest_digest
