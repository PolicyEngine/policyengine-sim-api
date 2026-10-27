"""Build the precompute's national and per-state datasets."""

from __future__ import annotations

import gc
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from policyengine_simulation_executor.precompute_models import (
    DatasetBuildResult,
    DatasetPlanEntry,
    StateDatasetBuildResult,
    StateDatasetPlanEntry,
)
from policyengine_simulation_executor.precompute_planning import (
    PRECOMPUTE_COUNTRY,
    state_dataset_targets,
)
from policyengine_simulation_executor.state_datasets import (
    state_dataset_relative_folder,
    year_file_reuse_problem,
)

if TYPE_CHECKING:
    from types import ModuleType

    from microdf import MicroDataFrame
    from policyengine.core.scoping_strategy import RowFilterStrategy


def build_dataset_impl(bucket: str, expected: DatasetPlanEntry) -> DatasetBuildResult:
    """Wave 1: build one single-year dataset and upload it."""
    from policyengine_simulation_executor.artifact_keys import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_keys.collect_dataset_identity
        collect_dataset_identity,
    )
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )
    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # resolved per call so tests can patch release_bundle.get_country_release_bundle
        get_country_release_bundle,
    )
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _country_module,
        resolve_data_folder,
    )

    identity = collect_dataset_identity(PRECOMPUTE_COUNTRY, expected.year)
    if identity.store_path != expected.path:
        msg = (
            "Planned and in-container dataset identities disagree "
            f"({expected.path} != {identity.store_path}); refusing to upload "
            "under a mismatched key."
        )
        raise RuntimeError(msg)

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
        msg = f"ensure_datasets produced no file at {local_file}"
        raise RuntimeError(msg)
    uploaded = ArtifactStore(bucket).upload_file(identity.store_path, local_file)
    return DatasetBuildResult(
        year=expected.year,
        path=identity.store_path,
        uploaded=uploaded,
        build_seconds=round(build_seconds, 1),
        size_bytes=local_file.stat().st_size,
    )


def _checked_state_targets(
    year: int, expected: list[StateDatasetPlanEntry]
) -> dict[str, RowFilterStrategy]:
    """Return each planned state's row filter, after re-deriving its key here.

    The planner and this builder run in different containers; both derive a
    state's key from the installed bundle. A disagreement means they see
    different bundles, so nothing is built rather than uploading a file under
    a key that does not describe it. A state without a row filter is refused
    here too, before anything is prepared.
    """
    from policyengine_simulation_executor.artifact_keys import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_keys.collect_state_dataset_identity
        collect_state_dataset_identity,
    )

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
            PRECOMPUTE_COUNTRY,
            year,
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
                f"Planned and in-container identities of {entry.region} {year} "
                f"disagree ({entry.path} != {identity.store_path}); refusing to "
                "build under a mismatched key."
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
    number). A household whose code names a district of another state would
    be missing from that district's results, so the build stops instead. A
    code that names no district, such as an unassigned 0, matches no district
    request on either path and changes no result. Both columns are labels;
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
    expected: list[StateDatasetPlanEntry],
    *,
    work_folder: Path | None = None,
) -> list[StateDatasetBuildResult]:
    """Build, check and upload one year's per-state datasets.

    Prepares the regional dataset's full-country year file once, then for
    each planned state keeps the rows its region's own scoping strategy keeps,
    writes them with .py's ``save()``, copies the year-file stamp, confirms
    that .py's reuse checks accept the file, and uploads it.
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

    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _country_module,
    )

    if not expected:
        return []
    started = time.monotonic()
    strategies = _checked_state_targets(year, expected)
    dataset = expected[0].dataset
    work_folder = work_folder or Path(
        tempfile.mkdtemp(prefix="policyengine-state-datasets-")
    )
    source_folder = str(work_folder / "source")
    country_module = _country_module(PRECOMPUTE_COUNTRY)

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

    store = ArtifactStore(bucket)
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
        results.append(
            StateDatasetBuildResult(
                year=year,
                state_code=entry.state_code,
                path=entry.path,
                uploaded=store.upload_file(entry.path, local_file),
                size_bytes=local_file.stat().st_size,
                build_seconds=round(time.monotonic() - started, 1),
            )
        )
    return results
