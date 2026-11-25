"""Build and upload one country's year files through its level handlers."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from policyengine_simulation_executor.precompute_models import (
    YearFileBuildResult,
    YearFilePlanEntry,
)
from policyengine_simulation_executor.precompute_planning import country_prebuilt

if TYPE_CHECKING:
    from policyengine_simulation_executor.stage12_cache.levels import YearFileTarget


def _checked_targets(
    year: int, expected: list[YearFilePlanEntry]
) -> list[YearFileTarget]:
    """Return the handler's target for each planned entry, after re-deriving its key.

    The planner and this builder run in different containers; both derive
    each key from the installed bundle. A disagreement means they see
    different bundles, so nothing is built rather than uploading a file under
    a key that does not describe it.
    """
    from policyengine_simulation_executor.artifact_keys import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_keys.year_file_identity
        year_file_identity,
    )

    countries = {entry.country for entry in expected}
    levels = {entry.level for entry in expected}
    datasets = {entry.dataset for entry in expected}
    if len(countries) > 1 or len(levels) > 1 or len(datasets) > 1:
        msg = (
            "One year-file build covers one country, level and dataset, not "
            f"{sorted(countries)}, {sorted(levels)}, {sorted(datasets)}"
        )
        raise ValueError(msg)
    (country,), (level,) = countries, levels
    handler = country_prebuilt(country).handlers[level]
    targets_by_key = {
        (target.dataset, None if target.subset is None else target.subset.code): target
        for target in handler.year_file_targets(year)
    }
    targets = []
    for entry in expected:
        if entry.year != year:
            msg = f"Year file entry {entry.path} is for {entry.year}, not {year}"
            raise ValueError(msg)
        target = targets_by_key.get((entry.dataset, entry.subset_code))
        if target is None:
            msg = (
                f"No {country} {level} year file target for {entry.dataset} "
                f"{entry.subset_code or 'all'} in this container"
            )
            raise RuntimeError(msg)
        identity = year_file_identity(target)
        if (identity.store_path, identity.filename, identity.folder) != (
            entry.path,
            entry.filename,
            entry.folder,
        ):
            msg = (
                f"Planned and in-container identities of {entry.dataset} "
                f"{entry.subset_code or 'all'} {year} disagree ({entry.path} != "
                f"{identity.store_path}); refusing to build under a mismatched key."
            )
            raise RuntimeError(msg)
        targets.append(target)
    return targets


def build_year_files_impl(
    bucket: str,
    year: int,
    expected: list[YearFilePlanEntry],
    *,
    work_folder: Path | None = None,
) -> list[YearFileBuildResult]:
    """Build, check and upload one year's year files of one level and dataset.

    The level's handler writes the files; each is uploaded write-once under
    its planned key, with its SHA-256 and size recorded on the stored object.
    """
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )

    if not expected:
        return []
    started = time.monotonic()
    targets = _checked_targets(year, expected)
    handler = country_prebuilt(expected[0].country).handlers[expected[0].level]
    work_folder = work_folder or Path(
        tempfile.mkdtemp(prefix="policyengine-year-files-")
    )
    written = handler.write_year_files(year, targets, work_folder)

    store = ArtifactStore(bucket)
    results = []
    for entry, target in zip(expected, targets, strict=True):
        local_file = written.get(target)
        if local_file is None or not local_file.exists():
            msg = f"The {entry.level} handler wrote no file for {entry.path}"
            raise RuntimeError(msg)
        results.append(
            YearFileBuildResult(
                year=year,
                path=entry.path,
                uploaded=store.upload_file(entry.path, local_file),
                size_bytes=local_file.stat().st_size,
                build_seconds=round(time.monotonic() - started, 1),
            )
        )
    return results
