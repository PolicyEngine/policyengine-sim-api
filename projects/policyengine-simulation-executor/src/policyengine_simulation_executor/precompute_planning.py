"""Plan the artifact precompute: cohorts, state dataset targets and the work to do."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from policyengine_simulation_executor.precompute_models import (
    BaselinePlanEntry,
    BundleVersionIdentity,
    DatasetPlanEntry,
    PrecomputePlan,
    StateDatasetPlanEntry,
    WorkSelection,
)
from policyengine_simulation_executor.state_datasets import (
    state_dataset_relative_folder,
)

if TYPE_CHECKING:
    from types import ModuleType

    from policyengine.core.scoping_strategy import RowFilterStrategy

    from policyengine_simulation_executor.artifact_keys import BaselineArtifactIdentity
    from policyengine_simulation_executor.simulation_runtime import RegionResolution

PRECOMPUTE_COUNTRY = "us"
# The user-facing priority order (2026 first); with parallel waves it only
# orders spawn submission, but keep it intentional.
PRECOMPUTE_YEARS = [2026, 2027, 2025]


def cohort_params(year: int, group: list[str]) -> dict:
    """Return the synthetic request a segmented-national child would receive."""
    return {
        "country": PRECOMPUTE_COUNTRY,
        "scope": "macro",
        "region_group": list(group),
        "time_period": year,
    }


def cohort_resolution(
    year: int, group: list[str]
) -> tuple[dict[str, Any], ModuleType, RegionResolution]:
    """Cohort request params, country module, and region resolution.

    The planner's identity (``cohort_identity``) and the worker's
    simulation (``_prepare_cohort_baseline``) must derive from ONE
    resolution or the in-container id checks abort the run; sharing this
    prologue removes the drift surface.
    """
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _country_module,
        resolve_region,
    )

    params = cohort_params(year, group)
    country_module = _country_module(PRECOMPUTE_COUNTRY)
    resolution = resolve_region(
        country_module=country_module,
        country=PRECOMPUTE_COUNTRY,
        params=params,
    )
    return params, country_module, resolution


def cohort_identity(year: int, group: list[str]) -> BaselineArtifactIdentity:
    """Identity via the executor's own resolution path (writer==reader)."""
    from policyengine_simulation_executor.baseline_artifacts import (
        BaselineIdentityRequest,
        qualifying_baseline_identity,
    )
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _resolve_dataset_selection,
    )

    params, _, resolution = cohort_resolution(year, group)
    dataset_selection = _resolve_dataset_selection(params, region_resolution=resolution)
    identity = qualifying_baseline_identity(
        BaselineIdentityRequest(
            params,
            country=PRECOMPUTE_COUNTRY,
            dataset_is_default=dataset_selection.is_default,
            policy=None,
            region_code=resolution.code,
            scoping_strategy=resolution.scoping_strategy,
            year=year,
        )
    )
    if identity is None:
        msg = (
            f"Cohort request for year {year} group {group} does not qualify "
            "for a deterministic baseline id — writer and reader predicates "
            "have diverged."
        )
        raise RuntimeError(msg)
    return identity


@dataclass(frozen=True)
class StateDatasetTarget:
    """A US state whose requests load a non-default dataset, and its row filter.

    The precompute bakes this state's rows of ``dataset`` as their own year
    file: the households whose ``filter_variable`` equals ``state_fips``.
    ``scoping_strategy`` is the region's own .py strategy, the one a request
    for the state applies, so a baked file holds exactly the rows the request
    would keep.
    """

    region: str
    state_code: str
    state_fips: int
    filter_variable: str
    dataset: str
    scoping_strategy: RowFilterStrategy | None = field(
        default=None, compare=False, repr=False
    )


def state_dataset_targets() -> list[StateDatasetTarget]:
    """Return every US state region that loads a non-default dataset, by state code.

    Each state is resolved exactly as a request for it is (``resolve_region``
    then ``_resolve_dataset_selection``), so a bundle that sends states back to
    the default dataset gets no state files.
    """
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _country_module,
        _resolve_dataset_selection,
        resolve_region,
    )

    country_module = _country_module(PRECOMPUTE_COUNTRY)
    targets = []
    for region in country_module.model.region_registry.get_by_type("state"):
        params = {
            "country": PRECOMPUTE_COUNTRY,
            "region": region.code,
            "time_period": PRECOMPUTE_YEARS[0],
        }
        resolution = resolve_region(
            country_module=country_module, country=PRECOMPUTE_COUNTRY, params=params
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
    return sorted(targets, key=lambda target: target.state_code)


def select_work(plan: PrecomputePlan, *, force: bool) -> WorkSelection:
    """Which plan entries need computing."""
    return WorkSelection(
        datasets=[entry for entry in plan.datasets if force or not entry.exists],
        baselines=[entry for entry in plan.baselines if force or not entry.exists],
        state_datasets=[
            entry for entry in plan.state_datasets if force or not entry.exists
        ],
    )


def plan_artifacts_impl(bucket: str) -> PrecomputePlan:
    """Compute every expected artifact identity and its store presence."""
    from policyengine_simulation_executor.artifact_keys import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_keys.collect_dataset_identity
        collect_dataset_identity,
        collect_state_dataset_identity,
    )
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )
    from policyengine_simulation_executor.national_partition import (  # noqa: PLC0415  # resolved per call so tests can patch national_partition.national_region_groups
        national_region_groups,
    )
    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # resolved per call so tests can patch release_bundle.get_country_release_bundle
        get_country_release_bundle,
    )

    store = ArtifactStore(bucket)
    groups = national_region_groups(PRECOMPUTE_COUNTRY)
    if not groups:
        msg = f"No national partition for {PRECOMPUTE_COUNTRY!r}"
        raise RuntimeError(msg)

    targets = state_dataset_targets()
    datasets: list[DatasetPlanEntry] = []
    baselines: list[BaselinePlanEntry] = []
    state_datasets: list[StateDatasetPlanEntry] = []
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
        for target in targets:
            state_identity = collect_state_dataset_identity(
                PRECOMPUTE_COUNTRY,
                year,
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

    bundle = get_country_release_bundle(PRECOMPUTE_COUNTRY)
    receipt = BundleVersionIdentity(
        policyengine_version=bundle.policyengine_version,
        model_version=bundle.model_version,
        data_version=bundle.data_version,
        data_artifact_revision=bundle.data_artifact_revision,
        default_dataset=bundle.default_dataset,
    )
    return PrecomputePlan(
        datasets=datasets,
        baselines=baselines,
        receipt=receipt,
        state_datasets=state_datasets,
    )
