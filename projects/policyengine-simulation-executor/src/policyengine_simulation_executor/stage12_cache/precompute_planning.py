"""Plan the Stage 12 cache through the level handlers: targets, identities and work.

The level handlers (``levels``) say which year files and baselines a country
builds for each year and where the image keeps them; ``cache_levels`` says
which of those levels this cache covers. This module turns each target into
the cache's own ``stage12-*`` identity and records whether the bucket holds it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, uuid5

from policyengine_simulation_contract.stage12_bundle import SUPPORTED_COUNTRIES
from policyengine_simulation_contract.stage12_execution import (
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    GeographySelection,
    PlannedSimulationExecutionInput,
    SimulationExecutionInput,
    SimulationRole,
    stage12_output_plan_sha256,
)

from policyengine_simulation_executor.simulation_runtime import (
    _country_module,
    _resolve_dataset_selection,
    resolve_region,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.stage12_runtime.cached_simulation import (
    qualifying_cache_identity,
)
from policyengine_simulation_executor.stage12_runtime.output_planning import (
    plan_simulation_input,
    resolve_simulation_output_plan,
)
from policyengine_simulation_executor.stage12_runtime.partition import US_REGION_GROUPS
from policyengine_simulation_executor.stage12_runtime.worker_inputs import (
    simulation_runtime_params,
)

from .cache_levels import cache_baseline_levels, cache_year_file_levels
from .keys import (
    Stage12BaselineCacheIdentity,
    canonical_digest,
    collect_dataset_identity,
    collect_state_dataset_identity,
)
from .levels import (
    CountryPrebuilt,
    DatasetLevel,
    RunScope,
    YearFileTarget,
    prebuilt_for,
    year_file_name,
)
from .models import (
    BaselinePlanEntry,
    CacheBundleIdentity,
    DatasetPlanEntry,
    PrecomputePlan,
    StateDatasetPlanEntry,
    WorkSelection,
)
from .store import Stage12CacheStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from policyengine_simulation_contract.stage12_bundle import (
        CountryId,
        ResolvedStage12Bundle,
    )

DETERMINISM_SCHEMA = "stage12-dv1"


def required_baseline_levels(country: str) -> tuple[DatasetLevel, ...]:
    """Return the cache's baseline levels for ``country``; ``RuntimeError`` if none.

    The precompute's determinism check, the manifest's publication and the
    deployed worker's check each probe a precomputed baseline, so a cache
    without one is refused before anything is built.
    """
    baseline_levels = cache_baseline_levels(country)
    if not baseline_levels:
        msg = (
            f"The {country} v2 cache needs at least one baseline level: its "
            "precompute, manifest and deploy check each probe a baseline"
        )
        raise RuntimeError(msg)
    return baseline_levels


def bundle_country(country: str) -> CountryId:
    """Return ``country`` as a Stage 12 bundle country; ``ValueError`` otherwise."""
    if country not in SUPPORTED_COUNTRIES:
        msg = f"{country!r} is not a Stage 12 bundle country"
        raise ValueError(msg)
    return cast("CountryId", country)


def country_prebuilt(country: str) -> CountryPrebuilt:
    """Return ``country``'s handler settings; ``RuntimeError`` for a country without."""
    prebuilt = prebuilt_for(country)
    if prebuilt is None:
        msg = f"No prebuilt-file handlers exist for {country}"
        raise RuntimeError(msg)
    return prebuilt


def _bundle_identity(country: CountryId) -> CacheBundleIdentity:
    resolved = load_stage12_bundle()
    country_bundle = next(
        item for item in resolved.bundle.countries if item.country == country
    )
    return CacheBundleIdentity(
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        policyengine_version=resolved.bundle.policyengine_version,
        core_package_version=resolved.bundle.core_package_version,
        country_package_version=country_bundle.country_package_version,
        data_package_version=country_bundle.data_package_version,
        data_artifact_revision=country_bundle.data_artifact_revision,
        default_dataset=country_bundle.default_dataset,
    )


def _planned_pair(
    year: int, country: CountryId = "us"
) -> tuple[PlannedSimulationExecutionInput, PlannedSimulationExecutionInput]:
    resolved = load_stage12_bundle()
    country_bundle = next(
        item for item in resolved.bundle.countries if item.country == country
    )
    dataset = next(
        item
        for item in country_bundle.datasets
        if item.identity == country_bundle.default_dataset
    )
    provenance = BundleProvenance(
        policyengine_version=resolved.bundle.policyengine_version,
        country_package_name=country_bundle.country_package_name,
        country_package_version=country_bundle.country_package_version,
        dataset=DatasetProvenance(
            identity=dataset.identity,
            uri=dataset.uri,
            artifact_revision=dataset.artifact_revision,
            data_package_name=country_bundle.data_package_name,
            data_package_version=country_bundle.data_package_version,
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
            geography=GeographySelection(country=country, region=country),
            bundle=provenance,
        )

    # Each side is planned as its simulation run stores its output, the plan
    # a report run's worker looks its current-law baseline up under.
    baseline, reform = (
        simulation(SimulationRole.BASELINE),
        simulation(SimulationRole.REFORM),
    )
    return (
        plan_simulation_input(baseline, resolve_simulation_output_plan(baseline)),
        plan_simulation_input(reform, resolve_simulation_output_plan(reform)),
    )


def scoped_planned_simulation(
    year: int,
    group: tuple[str, ...],
    *,
    reform: bool = False,
    country: CountryId = "us",
) -> PlannedSimulationExecutionInput:
    """Return the year's planned baseline, or reform, scoped to one region group."""
    baseline, reform_input = _planned_pair(year, country)
    selected = reform_input if reform else baseline
    return selected.model_copy(update={"options": {"region_group": list(group)}})


def baseline_identity(
    year: int, group: tuple[str, ...], *, country: CountryId = "us"
) -> tuple[PlannedSimulationExecutionInput, Stage12BaselineCacheIdentity]:
    """Return a planned baseline and the identity its worker reads it under.

    The identity comes from ``qualifying_cache_identity``, the rule the worker
    applies to the same request, so the precompute writes each baseline
    under the name the worker looks it up by. Raises ``RuntimeError`` when
    that rule gives the planned baseline no identity.
    """
    from policyengine_simulation_executor.spm import normalize_runtime_spm

    simulation = scoped_planned_simulation(year, group, country=country)
    params = simulation_runtime_params(simulation)
    country_module = _country_module(country)
    region = resolve_region(
        country_module=country_module, country=country, params=params
    )
    selection = _resolve_dataset_selection(params, region_resolution=region)
    identity = qualifying_cache_identity(
        simulation,
        resolved=load_stage12_bundle(),
        run=RunScope(country=country, year=year, region=region, dataset=selection),
        # The selection the worker builds the simulation with, which names
        # the file .py saves and loads.
        spm=normalize_runtime_spm(params),
    )
    if identity is None:
        msg = (
            f"The planned {country} {year} baseline for {list(group)} gets no "
            "cache identity: the precompute's and the worker's rules differ"
        )
        raise RuntimeError(msg)
    return simulation, identity


def determinism_record(baselines: Sequence[BaselinePlanEntry]) -> dict[str, Any]:
    """Return the record a passing determinism check writes for these baselines."""
    return {
        "schema": DETERMINISM_SCHEMA,
        "baselines": [entry.digest for entry in baselines],
    }


def determinism_record_path(
    baselines: Sequence[BaselinePlanEntry], country: str | None = None
) -> str:
    """Return the bucket path of the determinism record for these baselines.

    The path is named by the record's digest, so any change to the planned
    baselines needs a new passing check. ``country`` defaults to the one the
    baselines share; ``ValueError`` when there is none to take.
    """
    countries = {entry.country for entry in baselines}
    if country is None:
        if len(countries) != 1:
            msg = "A determinism record covers one country's baselines"
            raise ValueError(msg)
        (country,) = countries
    return (
        f"determinism/{country}/{canonical_digest(determinism_record(baselines))}.json"
    )


def _key_free_target_problem(
    resolved: ResolvedStage12Bundle, target: YearFileTarget
) -> str | None:
    """Return why the cache has no key for ``target``, or None when it has one."""
    if target.level is DatasetLevel.NATIONAL and target.subset is None:
        country_bundle = next(
            item for item in resolved.bundle.countries if item.country == target.country
        )
        if target.dataset != country_bundle.default_dataset:
            return f"a whole year file of {target.dataset}, not the default dataset"
        return None
    if target.level is DatasetLevel.STATE and target.subset is not None:
        return None
    return f"a {target.level} year file of {target.dataset}"


def _year_file_entry(
    resolved: ResolvedStage12Bundle,
    target: YearFileTarget,
    store: Stage12CacheStore,
) -> DatasetPlanEntry | StateDatasetPlanEntry:
    """Turn one handler year-file target into the cache's plan entry for it."""
    problem = _key_free_target_problem(resolved, target)
    if problem is not None:
        msg = f"The {target.country} cache has no v2 cache key for {problem}"
        raise RuntimeError(msg)
    country = bundle_country(target.country)
    if target.subset is None:
        identity = collect_dataset_identity(resolved, country=country, year=target.year)
    else:
        identity = collect_state_dataset_identity(
            resolved,
            country=country,
            year=target.year,
            dataset=target.dataset,
            state_code=target.subset.code,
            state_fips=target.subset.value,
            filter_variable=target.subset.variable,
        )
    if identity.filename != year_file_name(target):
        msg = (
            f"The cache would store {identity.filename} where the handlers look "
            f"for {year_file_name(target)}"
        )
        raise RuntimeError(msg)
    exists = store.exists(identity.store_path)
    if target.subset is None:
        return DatasetPlanEntry(
            country=target.country,
            year=target.year,
            digest=identity.digest,
            path=identity.store_path,
            filename=identity.filename,
            folder=target.folder,
            exists=exists,
        )
    return StateDatasetPlanEntry(
        country=target.country,
        year=target.year,
        state_code=target.subset.code,
        region=target.subset.region,
        dataset=target.dataset,
        digest=identity.digest,
        path=identity.store_path,
        filename=identity.filename,
        folder=target.folder,
        exists=exists,
    )


def _baseline_entries(
    prebuilt: CountryPrebuilt, year: int, store: Stage12CacheStore
) -> list[BaselinePlanEntry]:
    """Plan each baseline the cache precomputes for ``year``, in handler order."""
    entries = []
    country = bundle_country(prebuilt.country)
    for level in required_baseline_levels(prebuilt.country):
        if level is not DatasetLevel.STATE_GROUP:
            msg = (
                f"The {prebuilt.country} cache has no v2 cache key for {level} "
                "baselines"
            )
            raise RuntimeError(msg)
        for index, target in enumerate(prebuilt.handlers[level].baseline_targets(year)):
            group = tuple(target.request["region_group"])
            simulation, identity = baseline_identity(year, group, country=country)
            entries.append(
                BaselinePlanEntry(
                    country=prebuilt.country,
                    year=year,
                    segment_index=index,
                    region_codes=group,
                    digest=identity.digest,
                    path=identity.store_path,
                    filename=identity.filename,
                    folder=target.year_file.folder,
                    simulation_id=identity.simulation_id,
                    output_plan_sha256=stage12_output_plan_sha256(
                        simulation.output_plan
                    ),
                    exists=store.exists(identity.store_path),
                )
            )
    return entries


def plan_artifacts_impl(bucket: str, country: str = "us") -> PrecomputePlan:
    """Plan every year file and baseline the cache holds, and which the bucket has.

    The years come from the country's handler settings, the files from the
    handlers of the levels the cache covers (``cache_levels``). The plan also
    records whether a passing determinism check exists for these baselines.
    The v2 cache covers only the US until task 2.2 · 6, hence the default.
    """
    prebuilt = country_prebuilt(country)
    required_baseline_levels(country)
    resolved = load_stage12_bundle()
    store = Stage12CacheStore(bucket)
    datasets: list[DatasetPlanEntry] = []
    baselines: list[BaselinePlanEntry] = []
    state_datasets: list[StateDatasetPlanEntry] = []
    for year in prebuilt.years:
        for level in cache_year_file_levels(country):
            for target in prebuilt.handlers[level].year_file_targets(year):
                entry = _year_file_entry(resolved, target, store)
                if isinstance(entry, DatasetPlanEntry):
                    datasets.append(entry)
                else:
                    state_datasets.append(entry)
        baselines.extend(_baseline_entries(prebuilt, year, store))
    return PrecomputePlan(
        years=prebuilt.years,
        datasets=tuple(datasets),
        baselines=tuple(baselines),
        state_datasets=tuple(state_datasets),
        bundle=_bundle_identity(bundle_country(country)),
        partition_sha256=canonical_digest(
            {"groups": [list(group) for group in US_REGION_GROUPS]}
        ),
        determinism_verified=store.exists(determinism_record_path(baselines, country)),
    )


def expected_cache_contents(country: str) -> dict[str, Any]:
    """Return the years, year-file count and baseline count the cache holds.

    The image download step and the deployed worker's check require exactly
    these. They come from the handler settings and the cache's levels, and
    never load the country model: the deploying machine computes them.
    Year files here are whole-dataset ones (the manifest's ``dataset``
    entries); state files are counted per state by the worker check.
    """
    prebuilt = country_prebuilt(country)
    years = list(prebuilt.years)
    year_file_count = 0
    if DatasetLevel.NATIONAL in cache_year_file_levels(country):
        national = prebuilt.handlers[DatasetLevel.NATIONAL]
        year_file_count = sum(len(national.year_file_targets(year)) for year in years)
    baseline_count = sum(
        len(prebuilt.handlers[level].baseline_targets(year))
        for year in years
        for level in required_baseline_levels(country)
    )
    return {
        "years": years,
        "artifact_counts": {"dataset": year_file_count, "baseline": baseline_count},
    }


def select_work(plan: PrecomputePlan) -> WorkSelection:
    """Select only missing files: a stored cache file is never replaced."""
    return WorkSelection(
        datasets=tuple(entry for entry in plan.datasets if not entry.exists),
        baselines=tuple(entry for entry in plan.baselines if not entry.exists),
        state_datasets=tuple(
            entry for entry in plan.state_datasets if not entry.exists
        ),
    )
