"""Build and publish the independent Stage 12 US cache."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Callable
from uuid import NAMESPACE_URL, uuid5

from policyengine_simulation_contract.stage12_execution import (
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    GeographySelection,
    ReportAggregate,
    ReportExecutionInput,
    SimulationExecutionInput,
    SimulationRole,
    stage12_output_plan_sha256,
)

from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.stage12_runtime.output_planning import (
    plan_simulation_input,
    resolve_report_output_plan,
)
from policyengine_simulation_executor.stage12_runtime.partition import (
    US_REGION_GROUPS,
)

from .keys import (
    canonical_digest,
    collect_baseline_identity,
    collect_dataset_identity,
)
from .models import (
    BaselineBuildResult,
    BaselinePlanEntry,
    CacheBundleIdentity,
    CacheManifest,
    DatasetBuildResult,
    DatasetPlanEntry,
    DeterminismVerdict,
    ManifestArtifact,
    PrecomputePlan,
    RemoteFunction,
    WorkSelection,
)
from .store import Stage12CacheStore

PRECOMPUTE_YEARS = (2026, 2027, 2025)
MANIFEST_DIGEST_PREFIX = "STAGE12_CACHE_MANIFEST_DIGEST="


def manifest_digest_line(digest: str) -> str:
    return f"{MANIFEST_DIGEST_PREFIX}{digest}"


def _bundle_identity() -> CacheBundleIdentity:
    resolved = load_stage12_bundle()
    country = next(item for item in resolved.bundle.countries if item.country == "us")
    return CacheBundleIdentity(
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        policyengine_version=resolved.bundle.policyengine_version,
        core_package_version=resolved.bundle.core_package_version,
        country_package_version=country.country_package_version,
        data_package_version=country.data_package_version,
        data_artifact_revision=country.data_artifact_revision,
        default_dataset=country.default_dataset,
    )


def _planned_pair(year: int):
    resolved = load_stage12_bundle()
    country = next(item for item in resolved.bundle.countries if item.country == "us")
    dataset = next(
        item for item in country.datasets if item.identity == country.default_dataset
    )
    provenance = BundleProvenance(
        policyengine_version=resolved.bundle.policyengine_version,
        country_package_name=country.country_package_name,
        country_package_version=country.country_package_version,
        dataset=DatasetProvenance(
            identity=dataset.identity,
            uri=dataset.uri,
            artifact_revision=dataset.artifact_revision,
            data_package_name=country.data_package_name,
            data_package_version=country.data_package_version,
        ),
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
    )
    population = DatasetPopulationInput(
        artifact=DatasetArtifactReference(
            uri=dataset.uri,
            media_type=DatasetArtifactMediaType.HDF5,
            content_sha256=dataset.sha256,
        )
    )
    evaluation_id = uuid5(NAMESPACE_URL, f"stage12-cache:{year}")

    def simulation(role: SimulationRole) -> SimulationExecutionInput:
        return SimulationExecutionInput(
            evaluation_id=evaluation_id,
            simulation_execution_id=uuid5(evaluation_id, role.value),
            role=role,
            policy={},
            population=population,
            year=year,
            geography=GeographySelection(country="us", region="us"),
            bundle=provenance,
        )

    report = ReportExecutionInput(
        evaluation_id=evaluation_id,
        baseline=simulation(SimulationRole.BASELINE),
        reform=simulation(SimulationRole.REFORM),
        requested_aggregates=tuple(ReportAggregate),
    )
    plan = resolve_report_output_plan(report)
    return (
        plan_simulation_input(report.baseline, plan),
        plan_simulation_input(report.reform, plan),
    )


def _scoped_planned_simulation(
    year: int, group: tuple[str, ...], *, reform: bool = False
):
    baseline, reform_input = _planned_pair(year)
    selected = reform_input if reform else baseline
    return selected.model_copy(update={"options": {"region_group": list(group)}})


def _scope_key(year: int, group: tuple[str, ...]) -> str:
    from policyengine_simulation_executor.simulation_runtime import (
        _country_module,
        _resolve_region,
    )

    params = {
        "country": "us",
        "scope": "macro",
        "time_period": str(year),
        "region_group": list(group),
    }
    resolution = _resolve_region(
        country_module=_country_module("us"),
        country="us",
        params=params,
    )
    value = getattr(resolution.scoping_strategy, "cache_key", None)
    if not isinstance(value, str) or not value:
        raise RuntimeError("Stage 12 region group produced no cache identity")
    return value


def plan_artifacts_impl(bucket: str) -> PrecomputePlan:
    resolved = load_stage12_bundle()
    store = Stage12CacheStore(bucket)
    datasets = []
    baselines = []
    for year in PRECOMPUTE_YEARS:
        dataset = collect_dataset_identity(resolved, country="us", year=year)
        datasets.append(
            DatasetPlanEntry(
                year=year,
                digest=dataset.digest,
                path=dataset.store_path,
                filename=dataset.filename,
                exists=store.exists(dataset.store_path),
            )
        )
        for index, group in enumerate(US_REGION_GROUPS):
            simulation = _scoped_planned_simulation(year, group)
            identity = collect_baseline_identity(
                simulation,
                resolved=resolved,
                region_codes=group,
                scope_key=_scope_key(year, group),
            )
            baselines.append(
                BaselinePlanEntry(
                    year=year,
                    segment_index=index,
                    region_codes=group,
                    digest=identity.digest,
                    path=identity.store_path,
                    simulation_id=identity.simulation_id,
                    output_plan_sha256=stage12_output_plan_sha256(
                        simulation.output_plan
                    ),
                    exists=store.exists(identity.store_path),
                )
            )
    return PrecomputePlan(
        years=PRECOMPUTE_YEARS,
        datasets=tuple(datasets),
        baselines=tuple(baselines),
        bundle=_bundle_identity(),
        partition_sha256=canonical_digest(
            {"groups": [list(group) for group in US_REGION_GROUPS]}
        ),
    )


def select_work(plan: PrecomputePlan, *, force: bool) -> WorkSelection:
    return WorkSelection(
        datasets=tuple(entry for entry in plan.datasets if force or not entry.exists),
        baselines=tuple(entry for entry in plan.baselines if force or not entry.exists),
    )


def build_dataset_impl(bucket: str, expected: DatasetPlanEntry) -> DatasetBuildResult:
    from policyengine_simulation_executor.simulation_runtime import (
        _country_module,
        resolve_data_folder,
    )

    resolved = load_stage12_bundle()
    identity = collect_dataset_identity(resolved, country="us", year=expected.year)
    if identity.store_path != expected.path:
        raise RuntimeError("planned and runtime Stage 12 dataset identities differ")
    started = time.monotonic()
    _country_module("us").ensure_datasets(
        datasets=[identity.dataset],
        years=[expected.year],
        data_folder=resolve_data_folder(),
    )
    local_path = Path(resolve_data_folder()) / identity.filename
    uploaded, stored = Stage12CacheStore(bucket).upload_file(expected.path, local_path)
    return DatasetBuildResult(
        **stored.model_dump(),
        year=expected.year,
        uploaded=uploaded,
        build_seconds=round(time.monotonic() - started, 1),
    )


def _ensure_dataset_file(bucket: str, year: int) -> None:
    from policyengine_simulation_executor.simulation_runtime import resolve_data_folder

    identity = collect_dataset_identity(load_stage12_bundle(), country="us", year=year)
    local_path = Path(resolve_data_folder()) / identity.filename
    if not local_path.exists():
        Stage12CacheStore(bucket).download_file(identity.store_path, local_path)


def compute_baseline_impl(
    bucket: str, expected: BaselinePlanEntry
) -> BaselineBuildResult:
    from policyengine_simulation_executor.simulation_runtime import resolve_data_folder
    from policyengine_simulation_executor.stage12_runtime.simulation import (
        calculate_simulation_frames,
    )

    group = tuple(expected.region_codes)
    simulation = _scoped_planned_simulation(expected.year, group)
    identity = collect_baseline_identity(
        simulation,
        resolved=load_stage12_bundle(),
        region_codes=group,
        scope_key=_scope_key(expected.year, group),
    )
    if identity.store_path != expected.path:
        raise RuntimeError("planned and runtime Stage 12 baseline identities differ")
    _ensure_dataset_file(bucket, expected.year)
    started = time.monotonic()
    calculate_simulation_frames(simulation)
    local_path = Path(resolve_data_folder()) / identity.filename
    if not local_path.exists():
        raise RuntimeError("Stage 12 baseline calculation produced no cache file")
    uploaded, stored = Stage12CacheStore(bucket).upload_file(expected.path, local_path)
    return BaselineBuildResult(
        **stored.model_dump(),
        year=expected.year,
        segment_index=expected.segment_index,
        simulation_id=expected.simulation_id,
        cache_outcome="miss",
        uploaded=uploaded,
        compute_seconds=round(time.monotonic() - started, 1),
    )


def verify_determinism_impl(
    bucket: str, expected: BaselinePlanEntry
) -> DeterminismVerdict:
    from policyengine_simulation_executor.simulation_runtime import resolve_data_folder
    from policyengine_simulation_executor.stage12_runtime.simulation import (
        calculate_simulation_frames,
    )

    group = tuple(expected.region_codes)
    baseline = _scoped_planned_simulation(expected.year, group)
    reform = _scoped_planned_simulation(expected.year, group, reform=True)
    _ensure_dataset_file(bucket, expected.year)
    local_path = Path(resolve_data_folder()) / f"{expected.simulation_id}.h5"
    if not local_path.exists():
        Stage12CacheStore(bucket).download_file(expected.path, local_path)
    cached = calculate_simulation_frames(baseline).frames
    fresh = calculate_simulation_frames(reform).frames
    differences = []
    for entity in sorted(set(cached) | set(fresh)):
        left = cached.get(entity)
        right = fresh.get(entity)
        if left is None or right is None:
            differences.append(f"{entity}: missing on one side")
            continue
        for column in sorted(set(left) | set(right)):
            if column not in left or column not in right:
                differences.append(f"{entity}.{column}: only on one side")
            elif str(left[column].dtype) != str(right[column].dtype):
                differences.append(f"{entity}.{column}: dtype differs")
            elif not left[column].equals(right[column]):
                differences.append(f"{entity}.{column}: values differ")
    return DeterminismVerdict(
        equal=not differences,
        differences=tuple(differences[:50]),
    )


def build_manifest(plan: PrecomputePlan, store: Stage12CacheStore) -> CacheManifest:
    artifacts = []
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
                filename=f"{entry.simulation_id}.h5",
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
    store = Stage12CacheStore(bucket)
    return store.write_manifest(build_manifest(plan, store).canonical_payload())


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
    plan = PrecomputePlan.model_validate(plan_artifacts.remote(bucket))
    work = select_work(plan, force=force)
    dataset_calls = [
        build_dataset.spawn(bucket, entry.model_dump()) for entry in work.datasets
    ]
    for call in dataset_calls:
        DatasetBuildResult.model_validate(call.get())
    baseline_calls = [
        compute_baseline.spawn(bucket, entry.model_dump()) for entry in work.baselines
    ]
    for call in baseline_calls:
        BaselineBuildResult.model_validate(call.get())
    if work.baselines:
        verdict = DeterminismVerdict.model_validate(
            verify_determinism.remote(bucket, work.baselines[0].model_dump())
        )
        if not verdict.equal:
            raise SystemExit(
                "Stage 12 cache determinism validation failed: "
                + "; ".join(verdict.differences)
            )
    digest = publish_manifest.remote(bucket, plan.model_dump())
    if not isinstance(digest, str) or not digest:
        raise RuntimeError("Stage 12 cache manifest publisher returned no digest")
    echo(manifest_digest_line(digest))
    return digest
