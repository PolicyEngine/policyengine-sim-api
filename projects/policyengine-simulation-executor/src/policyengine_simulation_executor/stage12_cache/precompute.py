"""Build and publish the independent Stage 12 US cache."""

from __future__ import annotations

import gc
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from policyengine_simulation_contract.stage12_execution import (
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    GeographySelection,
    PlannedSimulationExecutionInput,
    ReportAggregate,
    ReportExecutionInput,
    SimulationExecutionInput,
    SimulationRole,
    stage12_output_plan_sha256,
)

from policyengine_simulation_executor import simulation_runtime
from policyengine_simulation_executor.simulation_runtime import (
    _country_module,
    _resolve_dataset_selection,
    _resolve_region,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.stage12_runtime import (
    simulation as stage12_simulation,
)
from policyengine_simulation_executor.stage12_runtime.cached_simulation import CACHE_HIT
from policyengine_simulation_executor.stage12_runtime.output_planning import (
    plan_simulation_input,
    resolve_report_output_plan,
)
from policyengine_simulation_executor.stage12_runtime.partition import (
    US_REGION_GROUPS,
)
from policyengine_simulation_executor.stage12_runtime.simulation import (
    simulation_runtime_params,
)

from .keys import (
    Stage12BaselineCacheIdentity,
    canonical_digest,
    collect_baseline_identity,
    collect_dataset_identity,
    collect_state_dataset_identity,
)
from .models import (
    BaselineBuildResult,
    BaselinePlanEntry,
    CacheBundleIdentity,
    CacheManifest,
    CacheManifestEntry,
    DatasetBuildResult,
    DatasetPlanEntry,
    DeterminismVerdict,
    ManifestArtifact,
    PrecomputePlan,
    StateDatasetBuildResult,
    StateDatasetManifestArtifact,
    StateDatasetPlanEntry,
    WorkSelection,
)
from .state_datasets import state_dataset_relative_folder, year_file_reuse_problem
from .store import Stage12CacheStore

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import modal
    import pandas as pd
    from microdf import MicroDataFrame

PRECOMPUTE_YEARS = (2026, 2027, 2025)
MANIFEST_DIGEST_PREFIX = "STAGE12_CACHE_MANIFEST_DIGEST="
DETERMINISM_SCHEMA = "stage12-dv1"


def manifest_digest_line(digest: str) -> str:
    """Return the output line that reports the published manifest digest."""
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


def _planned_pair(
    year: int,
) -> tuple[PlannedSimulationExecutionInput, PlannedSimulationExecutionInput]:
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
) -> PlannedSimulationExecutionInput:
    baseline, reform_input = _planned_pair(year)
    selected = reform_input if reform else baseline
    return selected.model_copy(update={"options": {"region_group": list(group)}})


def _scope_key(year: int, group: tuple[str, ...]) -> str:

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
        msg = "Stage 12 region group produced no cache identity"
        raise RuntimeError(msg)
    return value


def _baseline_identity(
    year: int, group: tuple[str, ...]
) -> tuple[PlannedSimulationExecutionInput, Stage12BaselineCacheIdentity]:
    """Return a planned baseline and the identity its worker reads it under."""
    from policyengine_simulation_executor.spm import normalize_runtime_spm

    simulation = _scoped_planned_simulation(year, group)
    identity = collect_baseline_identity(
        simulation,
        resolved=load_stage12_bundle(),
        region_codes=group,
        scope_key=_scope_key(year, group),
        # The selection the worker builds the simulation with, which names
        # the file .py saves and loads.
        spm=normalize_runtime_spm(simulation_runtime_params(simulation)),
    )
    return simulation, identity


def determinism_record(baselines: Sequence[BaselinePlanEntry]) -> dict[str, Any]:
    """Return the record a passing determinism check writes for these baselines."""
    return {
        "schema": DETERMINISM_SCHEMA,
        "baselines": [entry.digest for entry in baselines],
    }


def determinism_record_path(baselines: Sequence[BaselinePlanEntry]) -> str:
    """Return the bucket path of the determinism record for these baselines.

    The path is named by the record's digest, so any change to the planned
    baselines needs a new passing check.
    """
    return f"determinism/us/{canonical_digest(determinism_record(baselines))}.json"


@dataclass(frozen=True)
class StateDatasetTarget:
    """A US state whose requests load a non-default dataset, and its row filter.

    Stage 12 bakes this state's rows of ``dataset`` as their own year file:
    the households whose ``filter_variable`` equals ``state_fips``.
    ``scoping_strategy`` is the region's own .py strategy, the one a request
    for the state applies.
    """

    region: str
    state_code: str
    state_fips: int
    filter_variable: str
    dataset: str
    scoping_strategy: Any = field(default=None, compare=False, repr=False)


def state_dataset_targets() -> tuple[StateDatasetTarget, ...]:
    """Return every US state region that loads a non-default dataset, by state code.

    Each state is resolved as a Stage 12 worker resolves a request for it
    (``_resolve_region`` then ``_resolve_dataset_selection``), so a bundle
    that sends states back to the default dataset gets no state files.
    """
    country_module = _country_module("us")
    targets = []
    for region in country_module.model.region_registry.get_by_type("state"):
        params = {
            "country": "us",
            "region": region.code,
            "time_period": PRECOMPUTE_YEARS[0],
        }
        resolution = _resolve_region(
            country_module=country_module, country="us", params=params
        )
        selection = _resolve_dataset_selection(params, region_resolution=resolution)
        if selection.is_default:
            continue
        strategy = resolution.scoping_strategy
        variable_name = getattr(strategy, "variable_name", None)
        variable_value = getattr(strategy, "variable_value", None)
        if not variable_name or variable_value is None or not resolution.state_code:
            msg = (
                f"State region {resolution.code} loads {selection.name} but has no "
                "row filter and state code to build its state file from"
            )
            raise RuntimeError(msg)
        targets.append(
            StateDatasetTarget(
                region=resolution.code,
                state_code=resolution.state_code,
                state_fips=int(variable_value),
                filter_variable=variable_name,
                dataset=selection.name,
                scoping_strategy=strategy,
            )
        )
    return tuple(sorted(targets, key=lambda target: target.state_code))


def plan_artifacts_impl(bucket: str) -> PrecomputePlan:
    """Plan every dataset and baseline file and record which the bucket holds.

    The plan covers each precompute year and US region group, and records
    whether a passing determinism check exists for these baselines.
    """
    resolved = load_stage12_bundle()
    store = Stage12CacheStore(bucket)
    targets = state_dataset_targets()
    datasets = []
    baselines = []
    state_datasets = []
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
            simulation, identity = _baseline_identity(year, group)
            baselines.append(
                BaselinePlanEntry(
                    year=year,
                    segment_index=index,
                    region_codes=group,
                    digest=identity.digest,
                    path=identity.store_path,
                    filename=identity.filename,
                    simulation_id=identity.simulation_id,
                    output_plan_sha256=stage12_output_plan_sha256(
                        simulation.output_plan
                    ),
                    exists=store.exists(identity.store_path),
                )
            )
        for target in targets:
            state_identity = collect_state_dataset_identity(
                resolved,
                country="us",
                year=year,
                dataset=target.dataset,
                state_code=target.state_code,
                state_fips=target.state_fips,
                filter_variable=target.filter_variable,
            )
            state_datasets.append(
                StateDatasetPlanEntry(
                    year=year,
                    state_code=target.state_code,
                    region=target.region,
                    dataset=target.dataset,
                    digest=state_identity.digest,
                    path=state_identity.store_path,
                    filename=state_identity.filename,
                    folder=state_dataset_relative_folder(
                        target.dataset, target.state_code
                    ),
                    exists=store.exists(state_identity.store_path),
                )
            )
    return PrecomputePlan(
        years=PRECOMPUTE_YEARS,
        datasets=tuple(datasets),
        baselines=tuple(baselines),
        state_datasets=tuple(state_datasets),
        bundle=_bundle_identity(),
        partition_sha256=canonical_digest(
            {"groups": [list(group) for group in US_REGION_GROUPS]}
        ),
        determinism_verified=store.exists(determinism_record_path(baselines)),
    )


def select_work(plan: PrecomputePlan) -> WorkSelection:
    """Select only missing files: a stored cache file is never replaced."""
    return WorkSelection(
        datasets=tuple(entry for entry in plan.datasets if not entry.exists),
        baselines=tuple(entry for entry in plan.baselines if not entry.exists),
        state_datasets=tuple(
            entry for entry in plan.state_datasets if not entry.exists
        ),
    )


def build_dataset_impl(bucket: str, expected: DatasetPlanEntry) -> DatasetBuildResult:
    """Have .py put one planned dataset year in the data folder, then upload it.

    Raises ``RuntimeError`` when the installed bundle gives another identity
    than the plan, or when the cache path already holds other data.
    """
    resolved = load_stage12_bundle()
    identity = collect_dataset_identity(resolved, country="us", year=expected.year)
    if identity.store_path != expected.path:
        msg = "planned and runtime Stage 12 dataset identities differ"
        raise RuntimeError(msg)
    started = time.monotonic()
    _country_module("us").ensure_datasets(
        datasets=[identity.dataset],
        years=[expected.year],
        data_folder=simulation_runtime.resolve_data_folder(),
    )
    local_path = Path(simulation_runtime.resolve_data_folder()) / identity.filename
    uploaded, stored = Stage12CacheStore(bucket).upload_file(expected.path, local_path)
    return DatasetBuildResult(
        **stored.model_dump(),
        year=expected.year,
        uploaded=uploaded,
        build_seconds=round(time.monotonic() - started, 1),
    )


def _checked_state_targets(
    year: int, expected: Sequence[StateDatasetPlanEntry]
) -> dict[str, StateDatasetTarget]:
    """Return each planned state's target, after re-deriving its key here.

    Raises ``RuntimeError`` when the installed bundle gives another identity
    than the plan, so nothing is built under a key that does not describe it.
    """
    resolved = load_stage12_bundle()
    targets = {target.region: target for target in state_dataset_targets()}
    datasets = set()
    for entry in expected:
        if entry.year != year:
            msg = (
                f"State dataset entry for {entry.state_code} is for year "
                f"{entry.year}, not {year}"
            )
            raise ValueError(msg)
        target = targets.get(entry.region)
        if target is None:
            msg = f"No state dataset target for {entry.region} in this container"
            raise RuntimeError(msg)
        identity = collect_state_dataset_identity(
            resolved,
            country="us",
            year=year,
            dataset=target.dataset,
            state_code=target.state_code,
            state_fips=target.state_fips,
            filter_variable=target.filter_variable,
        )
        folder = state_dataset_relative_folder(target.dataset, target.state_code)
        if (identity.store_path, identity.filename, folder) != (
            entry.path,
            entry.filename,
            entry.folder,
        ):
            msg = (
                f"planned and runtime Stage 12 state dataset identities differ "
                f"for {entry.region} {year}"
            )
            raise RuntimeError(msg)
        datasets.add(target.dataset)
    if len(datasets) > 1:
        msg = f"One state dataset build covers one dataset, not {sorted(datasets)}"
        raise ValueError(msg)
    return {entry.region: targets[entry.region] for entry in expected}


def _require_districts_within_their_states(household: MicroDataFrame) -> None:
    """Refuse a dataset whose district codes name a different state.

    District requests read their state's file and filter it by
    ``congressional_district_geoid`` (state FIPS times 100 plus the district
    number). Both columns are labels; nothing here is weighted.
    """
    if "congressional_district_geoid" not in household.columns:
        return
    outside = (household["congressional_district_geoid"] // 100) != household[
        "state_fips"
    ]
    if outside.any():
        msg = (
            "Some households have a congressional district code outside their "
            "state; their state's file would not match the district's results"
        )
        raise RuntimeError(msg)


def build_state_datasets_impl(
    bucket: str,
    year: int,
    expected: Sequence[StateDatasetPlanEntry],
    *,
    work_folder: Path | None = None,
) -> tuple[StateDatasetBuildResult, ...]:
    """Build, check and upload one year's Stage 12 state datasets.

    Has .py prepare the regional dataset's full-country year file once, then
    for each planned state keeps the rows its region's own scoping strategy
    keeps, writes them with .py's ``save()``, copies the year-file stamp,
    confirms that .py's reuse checks accept the file, and uploads it.
    """
    from policyengine.tax_benefit_models.us.datasets import (
        PolicyEngineUSDataset,
        USYearData,
    )
    from policyengine.tax_benefit_models.us.legacy_inputs import (
        read_year_file_stamp,
        write_year_file_stamp,
    )
    from policyengine.tax_benefit_models.us.model import US_GROUP_ENTITIES

    if not expected:
        return ()
    targets = _checked_state_targets(year, expected)
    dataset = expected[0].dataset
    work_folder = work_folder or Path(
        tempfile.mkdtemp(prefix="policyengine-stage12-state-datasets-")
    )
    source_folder = str(work_folder / "source")
    country_module = _country_module("us")

    # Preparing the year file runs a Microsimulation over every state, whose
    # objects hold reference cycles; release them before filtering 51 copies.
    country_module.ensure_datasets(
        datasets=[dataset], years=[year], data_folder=source_folder
    )
    gc.collect()
    (source,) = country_module.load_datasets(
        datasets=[dataset], years=[year], data_folder=source_folder
    ).values()
    stamp = read_year_file_stamp(source.filepath)
    if stamp is None:
        msg = f"The prepared year file {source.filepath} has no year-file stamp"
        raise RuntimeError(msg)
    _require_districts_within_their_states(source.data.household)

    store = Stage12CacheStore(bucket)
    results = []
    for entry in expected:
        rows = targets[entry.region].scoping_strategy.apply(
            entity_data=source.data.entity_data,
            group_entities=US_GROUP_ENTITIES,
            year=year,
        )
        local_file = work_folder / entry.folder / entry.filename
        PolicyEngineUSDataset(
            name=f"{dataset}-{entry.state_code.lower()}-year-{year}",
            description=f"{entry.region} rows of {dataset} for {year}",
            filepath=str(local_file),
            year=year,
            metadata=dict(source.metadata),
            data=USYearData(**rows),
        ).save()
        # save() rewrites the file without the stamp, so stamp it afterwards,
        # as create_datasets does.
        write_year_file_stamp(local_file, stamp)
        problem = year_file_reuse_problem(local_file, dataset)
        if problem is not None:
            msg = f"ensure_datasets would not reuse {local_file}: {problem}"
            raise RuntimeError(msg)
        uploaded, stored = store.upload_file(entry.path, local_file)
        results.append(
            StateDatasetBuildResult(
                **stored.model_dump(),
                year=year,
                state_code=entry.state_code,
                uploaded=uploaded,
            )
        )
    return tuple(results)


def _ensure_dataset_file(bucket: str, year: int) -> None:

    identity = collect_dataset_identity(load_stage12_bundle(), country="us", year=year)
    local_path = Path(simulation_runtime.resolve_data_folder()) / identity.filename
    if not local_path.exists():
        Stage12CacheStore(bucket).download_file(identity.store_path, local_path)


def compute_baseline_impl(
    bucket: str, expected: BaselinePlanEntry
) -> BaselineBuildResult:
    """Calculate one planned baseline and upload the file .py saved.

    Raises ``RuntimeError`` when the runtime identity differs from the plan,
    when .py saved no file under the planned name, or when the cache path
    already holds other data.
    """
    simulation, identity = _baseline_identity(
        expected.year, tuple(expected.region_codes)
    )
    if identity.store_path != expected.path:
        msg = "planned and runtime Stage 12 baseline identities differ"
        raise RuntimeError(msg)
    _ensure_dataset_file(bucket, expected.year)
    started = time.monotonic()
    stage12_simulation.calculate_simulation_frames(simulation)
    local_path = Path(simulation_runtime.resolve_data_folder()) / identity.filename
    if not local_path.exists():
        msg = (
            f"Stage 12 baseline calculation produced no cache file {identity.filename}"
        )
        raise RuntimeError(msg)
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


def _frame_differences(
    cached: Mapping[str, pd.DataFrame], fresh: Mapping[str, pd.DataFrame]
) -> list[str]:
    """Describe every entity and column where two sets of frames differ, in order."""
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
    return differences


def verify_determinism_impl(
    bucket: str, plan: PrecomputePlan, expected: BaselinePlanEntry
) -> DeterminismVerdict:
    """Compare a stored baseline with a fresh calculation and record a pass."""
    if expected not in plan.baselines:
        msg = "Stage 12 determinism probe is not a planned baseline"
        raise RuntimeError(msg)
    store = Stage12CacheStore(bucket)
    group = tuple(expected.region_codes)
    baseline = _scoped_planned_simulation(expected.year, group)
    reform = _scoped_planned_simulation(expected.year, group, reform=True)
    _ensure_dataset_file(bucket, expected.year)
    local_path = Path(simulation_runtime.resolve_data_folder()) / expected.filename
    if not local_path.exists():
        store.download_file(expected.path, local_path)
    cached_calculation = stage12_simulation.calculate_simulation_frames(baseline)
    if cached_calculation.cache_outcome != CACHE_HIT:
        msg = (
            "Stage 12 determinism check did not load the stored baseline "
            f"(cache outcome {cached_calculation.cache_outcome})"
        )
        raise RuntimeError(msg)
    fresh = stage12_simulation.calculate_simulation_frames(reform).frames
    differences = _frame_differences(cached_calculation.frames, fresh)
    if not differences:
        store.write_json(
            determinism_record_path(plan.baselines),
            determinism_record(plan.baselines),
        )
    return DeterminismVerdict(
        equal=not differences,
        differences=tuple(differences[:50]),
    )


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
    year, and waited for only before the manifest is published, so the
    baselines never wait for them. Prints the manifest digest through
    ``echo`` and returns it. Exits with ``SystemExit`` when the determinism
    check fails.
    """
    plan = PrecomputePlan.model_validate(functions.plan_artifacts.remote(bucket))
    work = select_work(plan)
    dataset_calls = [
        functions.build_dataset.spawn(bucket, entry.model_dump())
        for entry in work.datasets
    ]
    state_entries_by_year: dict[int, list[dict[str, Any]]] = {}
    for entry in work.state_datasets:
        state_entries_by_year.setdefault(entry.year, []).append(entry.model_dump())
    state_calls = [
        functions.build_state_datasets.spawn(bucket, year, entries)
        for year, entries in state_entries_by_year.items()
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
    if state_calls:
        state_results = [
            StateDatasetBuildResult.model_validate(result)
            for call in state_calls
            for result in call.get()
        ]
        echo(
            f"State datasets: {len(state_results)} built, "
            f"{sum(result.uploaded for result in state_results)} uploaded, "
            f"{sum(result.size_bytes for result in state_results)} bytes"
        )
    digest = functions.publish_manifest.remote(bucket, plan.model_dump())
    if not isinstance(digest, str) or not digest:
        msg = "Stage 12 cache manifest publisher returned no digest"
        raise RuntimeError(msg)
    echo(manifest_digest_line(digest))
    return digest
