"""Compute the precompute's cohort baselines, and check that they are deterministic."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from policyengine_simulation_executor.artifact_keys import baseline_artifact_filename
from policyengine_simulation_executor.output_frames import EntityFrames
from policyengine_simulation_executor.precompute_models import (
    BaselineComputeResult,
    BaselinePlanEntry,
    DeterminismVerdict,
)
from policyengine_simulation_executor.precompute_planning import (
    PRECOMPUTE_COUNTRY,
    cohort_resolution,
)

if TYPE_CHECKING:
    from policyengine.core import Simulation

    from policyengine_simulation_executor.artifact_store import ArtifactStore


def _prepare_cohort_baseline(
    bucket: str, expected: BaselinePlanEntry
) -> tuple[ArtifactStore, Path, Simulation]:
    """Shared by compute and verify: dataset download + simulation build."""
    from policyengine_simulation_executor.artifact_keys import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_keys.collect_dataset_identity
        collect_dataset_identity,
    )
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _build_simulation,
        _load_dataset,
        _resolve_dataset_selection,
        resolve_data_folder,
    )

    store = ArtifactStore(bucket)
    data_folder = Path(resolve_data_folder())

    dataset_identity = collect_dataset_identity(PRECOMPUTE_COUNTRY, expected.year)
    local_dataset = data_folder / dataset_identity.filename
    if not local_dataset.exists():
        store.download_file(dataset_identity.store_path, local_dataset)

    params, country_module, resolution = cohort_resolution(
        expected.year, expected.group
    )
    dataset_selection = _resolve_dataset_selection(params, region_resolution=resolution)
    dataset = _load_dataset(
        params, country_module=country_module, selection=dataset_selection
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
    planned_storage_id = Path(expected.path).stem
    storage_id = getattr(baseline, "storage_id", baseline.id)
    if storage_id != planned_storage_id:
        msg = (
            "Planned and in-container baseline storage ids disagree "
            f"({planned_storage_id} != {storage_id}); refusing to act "
            "under a mismatched key."
        )
        raise RuntimeError(msg)

    # The extras economic_impact_analysis applies unconditionally before
    # ensure(); the artifact must carry them or every request would fail
    # the column guard and recompute.
    from policyengine.tax_benefit_models.us.analysis import (
        configure_budgetary_impact_variables,
    )

    configure_budgetary_impact_variables(baseline, baseline)
    return store, data_folder, baseline


def compute_baseline_impl(
    bucket: str, expected: BaselinePlanEntry
) -> BaselineComputeResult:
    """Wave 2: compute one cohort baseline and upload its output."""
    from policyengine_simulation_executor.baseline_artifacts import (
        ArtifactBaselineSimulation,
    )

    store, data_folder, baseline = _prepare_cohort_baseline(bucket, expected)
    if not isinstance(baseline, ArtifactBaselineSimulation):
        msg = (
            "Precompute built a plain Simulation — the qualifying predicate "
            "rejected the cohort request shape."
        )
        raise RuntimeError(msg)  # noqa: TRY004  # a precompute invariant failure, not a bad argument; raised across the compute_baseline Modal boundary and asserted by test_precompute

    started = time.monotonic()
    baseline.ensure()
    compute_seconds = time.monotonic() - started

    artifact_file = data_folder / baseline_artifact_filename(
        getattr(baseline, "storage_id", baseline.id)
    )
    if not artifact_file.exists():
        msg = f"ensure() left no artifact at {artifact_file}"
        raise RuntimeError(msg)
    uploaded = store.upload_file(expected.path, artifact_file)
    return BaselineComputeResult(
        year=expected.year,
        group=list(expected.group),
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

    store, data_folder, baseline = _prepare_cohort_baseline(bucket, expected)
    artifact_file = data_folder / baseline_artifact_filename(
        getattr(baseline, "storage_id", baseline.id)
    )
    if not artifact_file.exists():
        store.download_file(expected.path, artifact_file)
    baseline.ensure()
    # A cohort baseline is an ArtifactBaselineSimulation, which records the
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
