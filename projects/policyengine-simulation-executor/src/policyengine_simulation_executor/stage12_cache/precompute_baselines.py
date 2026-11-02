"""Compute the Stage 12 cache's baselines, and check that they are deterministic."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from policyengine_simulation_executor import simulation_runtime
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.stage12_runtime import (
    simulation as stage12_simulation,
)
from policyengine_simulation_executor.stage12_runtime.cached_simulation import CACHE_HIT

from .keys import collect_dataset_identity
from .models import (
    BaselineBuildResult,
    BaselinePlanEntry,
    DeterminismVerdict,
    PrecomputePlan,
)
from .precompute_planning import (
    baseline_identity,
    determinism_record,
    determinism_record_path,
    scoped_planned_simulation,
)
from .store import Stage12CacheStore

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pandas as pd


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
    simulation, identity = baseline_identity(
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
    baseline = scoped_planned_simulation(expected.year, group)
    reform = scoped_planned_simulation(expected.year, group, reform=True)
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
