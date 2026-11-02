"""Build the Stage 12 cache's national and per-state datasets."""

from __future__ import annotations

import gc
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from policyengine_simulation_executor import simulation_runtime
from policyengine_simulation_executor.simulation_runtime import _country_module
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle

from .keys import collect_dataset_identity, collect_state_dataset_identity
from .models import (
    DatasetBuildResult,
    DatasetPlanEntry,
    StateDatasetBuildResult,
    StateDatasetPlanEntry,
)
from .precompute_planning import state_dataset_targets
from .state_datasets import state_dataset_relative_folder, year_file_reuse_problem
from .store import Stage12CacheStore

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

    from microdf import MicroDataFrame
    from policyengine.core.scoping_strategy import RowFilterStrategy


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
) -> dict[str, RowFilterStrategy]:
    """Return each planned state's row filter, after re-deriving its key here.

    Raises ``RuntimeError`` when the installed bundle gives another identity
    than the plan, so nothing is built under a key that does not describe it,
    and when a state has no row filter, before anything is prepared.
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
        if target.scoping_strategy is None:
            msg = f"{entry.region} has no row filter to build its state file with"
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
    strategies = {}
    for entry in expected:
        strategy = targets[entry.region].scoping_strategy
        if strategy is not None:
            strategies[entry.region] = strategy
    return strategies


def _registered_district_geoids(country_module: ModuleType) -> set[int]:
    """Return the district codes the US congressional-district regions filter on."""
    return {
        int(region.scoping_strategy.variable_value)
        for region in country_module.model.region_registry.get_by_type(
            "congressional_district"
        )
    }


def _require_districts_within_their_states(
    household: MicroDataFrame, district_geoids: set[int]
) -> None:
    """Refuse a dataset in which a household sits in another state's district.

    District requests read their state's file and filter it by
    ``congressional_district_geoid`` (state FIPS times 100 plus the district
    number). A code that names no district, such as an unassigned 0, matches
    no district request and changes no result. Both columns are labels;
    nothing here is weighted.
    """
    if "congressional_district_geoid" not in household.columns:
        return
    district = household["congressional_district_geoid"]
    in_another_state = district.isin(sorted(district_geoids)) & (
        (district // 100) != household["state_fips"]
    )
    if in_another_state.any():
        msg = (
            "Some households have a congressional district code that names "
            "another state's district; their state's file would not match "
            "that district's results"
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
    started = time.monotonic()
    strategies = _checked_state_targets(year, expected)
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
    _require_districts_within_their_states(
        source.data.household, _registered_district_geoids(country_module)
    )

    store = Stage12CacheStore(bucket)
    results = []
    for entry in expected:
        rows = strategies[entry.region].apply(
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
                build_seconds=round(time.monotonic() - started, 1),
            )
        )
    return tuple(results)
