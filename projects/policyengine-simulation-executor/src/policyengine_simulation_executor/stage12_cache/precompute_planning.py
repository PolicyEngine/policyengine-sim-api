"""Plan the Stage 12 cache: baseline identities, state dataset targets and work."""

from __future__ import annotations

from dataclasses import dataclass, field
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

from policyengine_simulation_executor.simulation_runtime import (
    _country_module,
    _resolve_dataset_selection,
    _resolve_region,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.stage12_runtime.output_planning import (
    plan_simulation_input,
    resolve_report_output_plan,
)
from policyengine_simulation_executor.stage12_runtime.partition import US_REGION_GROUPS
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
    BaselinePlanEntry,
    CacheBundleIdentity,
    DatasetPlanEntry,
    PrecomputePlan,
    StateDatasetPlanEntry,
    WorkSelection,
)
from .state_datasets import state_dataset_relative_folder
from .store import Stage12CacheStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from policyengine.core.scoping_strategy import RowFilterStrategy

PRECOMPUTE_YEARS = (2026, 2027, 2025)

DETERMINISM_SCHEMA = "stage12-dv1"


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


def scoped_planned_simulation(
    year: int, group: tuple[str, ...], *, reform: bool = False
) -> PlannedSimulationExecutionInput:
    """Return the year's planned baseline, or reform, scoped to one region group."""
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


def baseline_identity(
    year: int, group: tuple[str, ...]
) -> tuple[PlannedSimulationExecutionInput, Stage12BaselineCacheIdentity]:
    """Return a planned baseline and the identity its worker reads it under."""
    from policyengine_simulation_executor.spm import normalize_runtime_spm

    simulation = scoped_planned_simulation(year, group)
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
    scoping_strategy: RowFilterStrategy | None = field(
        default=None, compare=False, repr=False
    )


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
            simulation, identity = baseline_identity(year, group)
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
