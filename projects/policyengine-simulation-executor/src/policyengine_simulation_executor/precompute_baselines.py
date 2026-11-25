"""Compute the precompute's current-law baselines and check they are deterministic."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from policyengine_simulation_executor.output_frames import EntityFrames
from policyengine_simulation_executor.precompute_models import (
    BaselineComputeResult,
    BaselinePlanEntry,
    DeterminismVerdict,
)
from policyengine_simulation_executor.precompute_planning import country_prebuilt
from policyengine_simulation_executor.request_policy import parse_year
from policyengine_simulation_executor.stage12_cache.levels import (
    RunScope,
    prebuilt_dataset_folder,
)

if TYPE_CHECKING:
    from policyengine.core import Simulation

    from policyengine_simulation_executor.artifact_store import ArtifactStore


def _prepare_baseline(
    bucket: str, expected: BaselinePlanEntry
) -> tuple[ArtifactStore, Path, Simulation]:
    """Shared by compute and verify: year-file download and simulation build.

    Downloads the entry's year file into its prebuilt folder when it is
    missing, then resolves the entry's request exactly as the runtime
    resolves a request and loads the year file through the level's prebuilt
    lookup, so the baseline is computed on, and saved beside, the file the
    image holds. Returns the store, the folder the baseline lands in and the
    simulation.
    """
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _build_simulation,
        _country_module,
        _load_dataset,
        _resolve_dataset_selection,
        resolve_data_folder,
        resolve_region,
    )

    store = ArtifactStore(bucket)
    data_folder = resolve_data_folder()
    folder = Path(data_folder) / expected.folder
    local_year_file = folder / expected.year_file_filename
    if not local_year_file.exists():
        store.download_file(expected.year_file_path, local_year_file)

    params = dict(expected.request)
    country_module = _country_module(expected.country)
    resolution = resolve_region(
        country_module=country_module, country=expected.country, params=params
    )
    dataset_selection = _resolve_dataset_selection(params, region_resolution=resolution)
    prebuilt = prebuilt_dataset_folder(
        RunScope(
            country=expected.country,
            year=parse_year(params),
            region=resolution,
            dataset=dataset_selection,
        ),
        data_folder,
    )
    if prebuilt is None or Path(prebuilt.folder) != folder:
        msg = (
            f"The {expected.label} baseline's year file {local_year_file} is not "
            "one its run would read; refusing to compute it elsewhere."
        )
        raise RuntimeError(msg)
    dataset = _load_dataset(
        params,
        country_module=country_module,
        selection=dataset_selection,
        data_folder=prebuilt.folder,
    )
    baseline = _build_simulation(
        params,
        dataset=dataset,
        dataset_selection=dataset_selection,
        policy=None,
        region=resolution,
    )
    if baseline.id != expected.simulation_id:
        msg = (
            "Planned and in-container baseline ids disagree "
            f"({expected.simulation_id} != {baseline.id}); refusing to act "
            "under a mismatched key."
        )
        raise RuntimeError(msg)
    storage_id = getattr(baseline, "storage_id", baseline.id)
    if storage_id != expected.storage_id:
        msg = (
            "Planned and in-container baseline storage ids disagree "
            f"({expected.storage_id} != {storage_id}); refusing to act "
            "under a mismatched key."
        )
        raise RuntimeError(msg)

    country_prebuilt(expected.country).prepare_baseline_outputs(baseline)
    return store, folder, baseline


def compute_baseline_impl(
    bucket: str, expected: BaselinePlanEntry
) -> BaselineComputeResult:
    """Compute one current-law baseline and upload its output."""
    from policyengine_simulation_executor.baseline_artifacts import (
        ArtifactBaselineSimulation,
    )

    store, folder, baseline = _prepare_baseline(bucket, expected)
    if not isinstance(baseline, ArtifactBaselineSimulation):
        msg = (
            "Precompute built a plain Simulation — the qualifying predicate "
            "rejected the baseline request."
        )
        raise RuntimeError(msg)  # noqa: TRY004  # a precompute invariant failure, not a bad argument; raised across the compute_baseline Modal boundary and asserted by test_precompute

    started = time.monotonic()
    baseline.ensure()
    compute_seconds = time.monotonic() - started

    artifact_file = folder / f"{expected.storage_id}.h5"
    if not artifact_file.exists():
        msg = f"ensure() left no artifact at {artifact_file}"
        raise RuntimeError(msg)
    uploaded = store.upload_file(expected.path, artifact_file)
    return BaselineComputeResult(
        year=expected.year,
        label=expected.label,
        simulation_id=baseline.id,
        outcome=baseline.artifact_outcome,
        uploaded=uploaded,
        compute_seconds=round(compute_seconds, 1),
        size_bytes=artifact_file.stat().st_size,
    )


def verify_determinism_impl(
    bucket: str, expected: BaselinePlanEntry
) -> DeterminismVerdict:
    """Load the uploaded artifact, recompute independently, compare frames.

    Exact comparison of every entity column (values and dtypes) between the
    store artifact (downloaded and loaded through ``ensure()``, so the save/
    load round-trip is under test too) and a fresh independent run — the
    direct check of "same key, same bytes". File-level byte equality is
    deliberately not used (HDF5 container metadata is not guaranteed
    stable).
    """
    from policyengine.core import (  # noqa: PLC0415  # resolved per call so tests can patch policyengine.core.Simulation
        Simulation,
    )

    from policyengine_simulation_executor.baseline_artifacts import (
        OUTCOME_HIT,
    )

    store, folder, baseline = _prepare_baseline(bucket, expected)
    artifact_file = folder / f"{expected.storage_id}.h5"
    if not artifact_file.exists():
        store.download_file(expected.path, artifact_file)
    baseline.ensure()
    # A planned baseline is an ArtifactBaselineSimulation, which records the
    # outcome; the attribute isn't on the Simulation type it is declared as.
    outcome = getattr(baseline, "artifact_outcome", None)
    if outcome != OUTCOME_HIT:
        msg = (
            "Determinism gate could not load the artifact it verifies "
            f"(outcome={outcome})"
        )
        raise RuntimeError(msg)

    selection = getattr(baseline, "spm_config", None)
    fresh = Simulation(
        **({"spm": selection} if selection is not None else {}),
        dataset=baseline.dataset,
        tax_benefit_model_version=baseline.tax_benefit_model_version,
        policy=None,
        scoping_strategy=baseline.scoping_strategy,
        extra_variables=dict(baseline.extra_variables),
    )
    fresh.run()

    missing_frames = "Determinism gate found a simulation with no output entity frames"
    if baseline.output_dataset is None or not isinstance(
        baseline.output_dataset.data, EntityFrames
    ):
        raise TypeError(missing_frames)
    if fresh.output_dataset is None or not isinstance(
        fresh.output_dataset.data, EntityFrames
    ):
        raise TypeError(missing_frames)
    differences = []
    loaded = baseline.output_dataset.data.entity_data
    recomputed = fresh.output_dataset.data.entity_data
    for entity in sorted(set(loaded) | set(recomputed)):
        left, right = loaded.get(entity), recomputed.get(entity)
        if left is None or right is None:
            differences.append(f"{entity}: missing on one side")
            continue
        left_cols, right_cols = set(left.columns), set(right.columns)
        differences.extend(
            f"{entity}.{column}: only on one side"
            for column in sorted(left_cols ^ right_cols)
        )
        for column in sorted(left_cols & right_cols):
            if str(left[column].dtype) != str(right[column].dtype):
                differences.append(f"{entity}.{column}: dtype differs")
            elif not left[column].equals(right[column]):
                differences.append(f"{entity}.{column}: values differ")
    return DeterminismVerdict(equal=not differences, differences=differences[:50])
