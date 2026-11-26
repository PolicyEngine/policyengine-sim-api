"""Build the Stage 12 cache's year files through the level handlers, then upload them.

The handlers write the files: the national handler has .py prepare the
default dataset's year file, and the state handler filters each state's rows
of the state-level dataset's year file and checks .py would reuse the result.
This module checks each planned entry's key in the container, has the handler
write the files and uploads them under their keys.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle

from .keys import collect_dataset_identity, collect_state_dataset_identity
from .levels import DatasetLevel
from .models import (
    DatasetBuildResult,
    DatasetPlanEntry,
    StateDatasetBuildResult,
    StateDatasetPlanEntry,
)
from .precompute_planning import bundle_country, country_prebuilt
from .store import Stage12CacheStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .levels import YearFileTarget


def build_dataset_impl(bucket: str, expected: DatasetPlanEntry) -> DatasetBuildResult:
    """Have the national handler write one planned year file, then upload it.

    Raises ``RuntimeError`` when the installed bundle gives another identity
    than the plan, when the handler plans no year file in the entry's folder,
    or when the cache path already holds other data.
    """
    identity = collect_dataset_identity(
        load_stage12_bundle(),
        country=bundle_country(expected.country),
        year=expected.year,
    )
    if identity.store_path != expected.path:
        msg = "planned and runtime Stage 12 dataset identities differ"
        raise RuntimeError(msg)
    handler = country_prebuilt(expected.country).handlers[DatasetLevel.NATIONAL]
    targets = [
        target
        for target in handler.year_file_targets(expected.year)
        if target.folder == expected.folder
    ]
    if len(targets) != 1:
        msg = (
            f"The national handler plans no {expected.year} year file in "
            f"{expected.folder}"
        )
        raise RuntimeError(msg)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix="policyengine-stage12-datasets-"
    ) as work_folder:
        written = handler.write_year_files(expected.year, targets, Path(work_folder))
        uploaded, stored = Stage12CacheStore(bucket).upload_file(
            expected.path, written[targets[0]]
        )
    return DatasetBuildResult(
        **stored.model_dump(),
        year=expected.year,
        uploaded=uploaded,
        build_seconds=round(time.monotonic() - started, 1),
    )


def _planned_state_targets(
    year: int, expected: Sequence[StateDatasetPlanEntry]
) -> list[YearFileTarget]:
    """Return the state handler's target for each planned entry, in entry order.

    Each entry's key is derived again here, from the installed bundle and the
    handler's target. Raises ``ValueError`` for an entry of another year or
    country, and ``RuntimeError`` when the handler has no target for an
    entry's state or the keys differ, so nothing is built under a key that
    does not describe it.
    """
    countries = {entry.country for entry in expected}
    if len(countries) != 1:
        msg = f"One state file build covers one country, not {sorted(countries)}"
        raise ValueError(msg)
    (country,) = countries
    for entry in expected:
        if entry.year != year:
            msg = (
                f"State dataset entry for {entry.state_code} is for year "
                f"{entry.year}, not {year}"
            )
            raise ValueError(msg)
    handler = country_prebuilt(country).handlers[DatasetLevel.STATE]
    by_region = {
        target.subset.region: target
        for target in handler.year_file_targets(year)
        if target.subset is not None
    }
    resolved = load_stage12_bundle()
    targets = []
    for entry in expected:
        target = by_region.get(entry.region)
        if target is None or target.subset is None:
            msg = f"No state dataset target for {entry.region} in this container"
            raise RuntimeError(msg)
        identity = collect_state_dataset_identity(
            resolved,
            country=bundle_country(country),
            year=year,
            dataset=target.dataset,
            state_code=target.subset.code,
            state_fips=target.subset.value,
            filter_variable=target.subset.variable,
        )
        if (identity.store_path, identity.filename, target.folder) != (
            entry.path,
            entry.filename,
            entry.folder,
        ):
            msg = (
                f"planned and runtime Stage 12 state dataset identities differ "
                f"for {entry.region} {year}"
            )
            raise RuntimeError(msg)
        targets.append(target)
    return targets


def build_state_datasets_impl(
    bucket: str,
    year: int,
    expected: Sequence[StateDatasetPlanEntry],
    *,
    work_folder: Path | None = None,
) -> tuple[StateDatasetBuildResult, ...]:
    """Build, check and upload one year's Stage 12 state datasets.

    The state handler prepares the state-level dataset's full year file once,
    writes each planned state's rows as its own year file and confirms .py
    would reuse it; each file is then uploaded under its planned key.
    """
    if not expected:
        return ()
    started = time.monotonic()
    targets = _planned_state_targets(year, expected)
    work_folder = work_folder or Path(
        tempfile.mkdtemp(prefix="policyengine-stage12-state-datasets-")
    )
    handler = country_prebuilt(expected[0].country).handlers[DatasetLevel.STATE]
    written = handler.write_year_files(year, targets, work_folder)
    store = Stage12CacheStore(bucket)
    results = []
    for entry, target in zip(expected, targets, strict=True):
        uploaded, stored = store.upload_file(entry.path, written[target])
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
