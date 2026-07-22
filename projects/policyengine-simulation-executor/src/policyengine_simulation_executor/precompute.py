"""Library half of the artifact precompute (the write path of the pipeline).

``src/modal/precompute_app.py`` holds only the Modal plumbing — app, image,
function declarations, local entrypoint — and every function body lives
here, mirroring the deployed app's split (``run_simulation`` ->
``simulation_runtime.run_simulation_impl``). Keeping the logic in the
executor package makes it testable without Modal fakes and importable by
the CI deploy job and the GC planner.

The store is content-addressed, so a precompute run is idempotent: it
plans against the store and computes only misses; a re-run against a warm
store is a no-op. ``force`` recomputes everything but CANNOT replace
existing store objects (uploads are write-once) — healing a bad artifact
means deleting its object first, then re-running.

Cohort baselines are built through the executor's own request path
(``_resolve_region`` + ``_build_simulation`` on a synthetic child request),
so scoping, extra variables, and the deterministic simulation id match the
production child's by construction, not by convention — and a regression
test asserts the writer and reader derive identical identities. All
identity collection and store I/O must run inside containers: the bundle
receipt that the keys digest lives in the image, not on the deploying
machine.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

PRECOMPUTE_COUNTRY = "us"
# The user-facing priority order (2026 first); with parallel waves it only
# orders spawn submission, but keep it intentional.
PRECOMPUTE_YEARS = [2026, 2027, 2025]

MANIFEST_SCHEMA = "mf1"

# The stdout contract the CI deploy job parses; keep format changes in
# lockstep with the workflow side (covered by a regression test).
MANIFEST_DIGEST_PREFIX = "MANIFEST_DIGEST="


def manifest_digest_line(digest: str) -> str:
    return f"{MANIFEST_DIGEST_PREFIX}{digest}"


def cohort_params(year: int, group: list[str]) -> dict:
    """The synthetic request a segmented-national child would receive."""
    return {
        "country": PRECOMPUTE_COUNTRY,
        "scope": "macro",
        "region_group": list(group),
        "time_period": year,
    }


def cohort_identity(year: int, group: list[str]):
    """Identity via the executor's own resolution path (writer==reader)."""
    from policyengine_simulation_executor.baseline_artifacts import (
        qualifying_baseline_identity,
    )
    from policyengine_simulation_executor.simulation_runtime import (
        _country_module,
        _resolve_region,
    )

    params = cohort_params(year, group)
    country_module = _country_module(PRECOMPUTE_COUNTRY)
    resolution = _resolve_region(
        country_module=country_module,
        country=PRECOMPUTE_COUNTRY,
        params=params,
    )
    identity = qualifying_baseline_identity(
        params,
        country=PRECOMPUTE_COUNTRY,
        policy=None,
        region_code=resolution.code,
        scoping_strategy=resolution.scoping_strategy,
        year=year,
    )
    if identity is None:
        raise RuntimeError(
            f"Cohort request for year {year} group {group} does not qualify "
            "for a deterministic baseline id — writer and reader predicates "
            "have diverged."
        )
    return identity


def select_work(plan: dict, *, force: bool) -> dict:
    """Pure work selection: which plan entries need computing."""
    datasets = [entry for entry in plan["datasets"] if force or not entry["exists"]]
    baselines = [entry for entry in plan["baselines"] if force or not entry["exists"]]
    return {"datasets": datasets, "baselines": baselines}


def build_manifest_payload(plan: dict) -> dict:
    """The deploy manifest: receipt + every artifact the image must fetch."""
    artifacts = [
        {
            "type": "dataset",
            "path": entry["path"],
            "filename": entry["filename"],
            "year": entry["year"],
            "digest": entry["digest"],
        }
        for entry in plan["datasets"]
    ] + [
        {
            "type": "baseline",
            "path": entry["path"],
            "filename": f"{entry['simulation_id']}.h5",
            "year": entry["year"],
            "digest": entry["digest"],
        }
        for entry in plan["baselines"]
    ]
    return {
        "schema": MANIFEST_SCHEMA,
        "country": PRECOMPUTE_COUNTRY,
        "receipt": plan["receipt"],
        "artifacts": artifacts,
    }


def plan_artifacts_impl(bucket: str) -> dict:
    """Compute every expected artifact identity and its store presence."""
    from policyengine_simulation_executor.artifact_keys import (
        collect_dataset_identity,
    )
    from policyengine_simulation_executor.artifact_store import ArtifactStore
    from policyengine_simulation_executor.national_partition import (
        national_region_groups,
    )
    from policyengine_simulation_executor.release_bundle import (
        get_country_release_bundle,
    )

    store = ArtifactStore(bucket)
    groups = national_region_groups(PRECOMPUTE_COUNTRY)
    if not groups:
        raise RuntimeError(f"No national partition for {PRECOMPUTE_COUNTRY!r}")

    datasets = []
    baselines = []
    for year in PRECOMPUTE_YEARS:
        dataset_identity = collect_dataset_identity(PRECOMPUTE_COUNTRY, year)
        datasets.append(
            {
                "year": year,
                "digest": dataset_identity.digest,
                "path": dataset_identity.store_path,
                "filename": dataset_identity.filename,
                "exists": store.exists(dataset_identity.store_path),
            }
        )
        for group in groups:
            identity = cohort_identity(year, group)
            baselines.append(
                {
                    "year": year,
                    "group": list(group),
                    "region": identity.region,
                    "digest": identity.digest,
                    "path": identity.store_path,
                    "simulation_id": identity.simulation_id,
                    "exists": store.exists(identity.store_path),
                }
            )

    bundle = get_country_release_bundle(PRECOMPUTE_COUNTRY)
    receipt = {
        "policyengine_version": bundle.policyengine_version,
        "model_version": bundle.model_version,
        "data_version": bundle.data_version,
        "data_artifact_revision": bundle.data_artifact_revision,
        "default_dataset": bundle.default_dataset,
    }
    return {"datasets": datasets, "baselines": baselines, "receipt": receipt}


def build_dataset_impl(bucket: str, year: int, expected_path: str) -> dict:
    """Wave 1: build one single-year dataset and upload it."""
    import os
    from pathlib import Path

    from policyengine_simulation_executor.artifact_keys import (
        collect_dataset_identity,
    )
    from policyengine_simulation_executor.artifact_store import ArtifactStore
    from policyengine_simulation_executor.release_bundle import (
        get_country_release_bundle,
    )
    from policyengine_simulation_executor.simulation_runtime import (
        _country_module,
    )

    identity = collect_dataset_identity(PRECOMPUTE_COUNTRY, year)
    if identity.store_path != expected_path:
        raise RuntimeError(
            "Planned and in-container dataset identities disagree "
            f"({expected_path} != {identity.store_path}); refusing to upload "
            "under a mismatched key."
        )

    data_folder = os.environ.get("POLICYENGINE_DATA_FOLDER", "/opt/policyengine/data")
    country_module = _country_module(PRECOMPUTE_COUNTRY)
    started = time.monotonic()
    country_module.ensure_datasets(
        datasets=[get_country_release_bundle(PRECOMPUTE_COUNTRY).default_dataset],
        years=[year],
        data_folder=data_folder,
    )
    build_seconds = time.monotonic() - started

    local_file = Path(data_folder) / identity.filename
    if not local_file.exists():
        raise RuntimeError(f"ensure_datasets produced no file at {local_file}")
    uploaded = ArtifactStore(bucket).upload_file(identity.store_path, local_file)
    return {
        "year": year,
        "path": identity.store_path,
        "uploaded": uploaded,
        "build_seconds": round(build_seconds, 1),
        "size_bytes": local_file.stat().st_size,
    }


def _prepare_cohort_baseline(bucket: str, year: int, group: list[str]):
    """Shared by compute and verify: dataset download + simulation build."""
    import os
    from pathlib import Path

    from policyengine_simulation_executor.artifact_keys import (
        collect_dataset_identity,
    )
    from policyengine_simulation_executor.artifact_store import ArtifactStore
    from policyengine_simulation_executor.simulation_runtime import (
        _build_simulation,
        _country_module,
        _load_dataset,
        _resolve_region,
    )

    store = ArtifactStore(bucket)
    data_folder = Path(
        os.environ.get("POLICYENGINE_DATA_FOLDER", "/opt/policyengine/data")
    )

    dataset_identity = collect_dataset_identity(PRECOMPUTE_COUNTRY, year)
    local_dataset = data_folder / dataset_identity.filename
    if not local_dataset.exists():
        store.download_file(dataset_identity.store_path, local_dataset)

    params = cohort_params(year, group)
    country_module = _country_module(PRECOMPUTE_COUNTRY)
    resolution = _resolve_region(
        country_module=country_module,
        country=PRECOMPUTE_COUNTRY,
        params=params,
    )
    dataset = _load_dataset(
        params, country_module=country_module, region_resolution=resolution
    )
    baseline = _build_simulation(
        params,
        dataset=dataset,
        policy=None,
        scoping_strategy=resolution.scoping_strategy,
        region_code=resolution.code,
    )

    # The extras economic_impact_analysis applies unconditionally before
    # ensure(); the artifact must carry them or every request would fail
    # the column guard and recompute.
    from policyengine.tax_benefit_models.us.analysis import (
        configure_budgetary_impact_variables,
    )

    configure_budgetary_impact_variables(baseline, baseline)
    return store, data_folder, baseline


def compute_baseline_impl(
    bucket: str, year: int, group: list[str], expected: dict
) -> dict:
    """Wave 2: compute one cohort baseline and upload its output."""
    from policyengine_simulation_executor.baseline_artifacts import (
        ArtifactBaselineSimulation,
    )

    store, data_folder, baseline = _prepare_cohort_baseline(bucket, year, group)
    if not isinstance(baseline, ArtifactBaselineSimulation):
        raise RuntimeError(
            "Precompute built a plain Simulation — the qualifying predicate "
            "rejected the cohort request shape."
        )
    if baseline.id != expected["simulation_id"]:
        raise RuntimeError(
            "Planned and in-container baseline ids disagree "
            f"({expected['simulation_id']} != {baseline.id}); refusing to "
            "upload under a mismatched key."
        )

    started = time.monotonic()
    baseline.ensure()
    compute_seconds = time.monotonic() - started

    artifact_file = data_folder / f"{baseline.id}.h5"
    if not artifact_file.exists():
        raise RuntimeError(f"ensure() left no artifact at {artifact_file}")
    uploaded = store.upload_file(expected["path"], artifact_file)
    return {
        "year": year,
        "group": list(group),
        "simulation_id": baseline.id,
        "outcome": baseline.artifact_outcome,
        "uploaded": uploaded,
        "compute_seconds": round(compute_seconds, 1),
        "size_bytes": artifact_file.stat().st_size,
    }


def verify_determinism_impl(
    bucket: str, year: int, group: list[str], expected: dict
) -> dict:
    """Load the uploaded artifact, recompute independently, compare frames.

    Exact comparison of every entity column (values and dtypes) between the
    store artifact (downloaded and loaded through ``ensure()``, so the save/
    load round-trip is under test too) and a fresh independent run — the
    direct check of "same key, same bytes". File-level byte equality is
    deliberately not used (HDF5 container metadata is not guaranteed
    stable).
    """
    from policyengine.core import Simulation

    store, data_folder, baseline = _prepare_cohort_baseline(bucket, year, group)
    if baseline.id != expected["simulation_id"]:
        raise RuntimeError(
            "Planned and in-container baseline ids disagree "
            f"({expected['simulation_id']} != {baseline.id})"
        )
    artifact_file = data_folder / f"{baseline.id}.h5"
    if not artifact_file.exists():
        store.download_file(expected["path"], artifact_file)
    baseline.ensure()
    if baseline.artifact_outcome != "hit":
        raise RuntimeError(
            "Determinism gate could not load the artifact it verifies "
            f"(outcome={baseline.artifact_outcome})"
        )

    fresh = Simulation(
        dataset=baseline.dataset,
        tax_benefit_model_version=baseline.tax_benefit_model_version,
        policy=None,
        scoping_strategy=baseline.scoping_strategy,
        extra_variables=dict(baseline.extra_variables),
    )
    fresh.run()

    differences = []
    loaded = baseline.output_dataset.data.entity_data
    recomputed = fresh.output_dataset.data.entity_data
    for entity in sorted(set(loaded) | set(recomputed)):
        left, right = loaded.get(entity), recomputed.get(entity)
        if left is None or right is None:
            differences.append(f"{entity}: missing on one side")
            continue
        left_cols, right_cols = set(left.columns), set(right.columns)
        for column in sorted(left_cols ^ right_cols):
            differences.append(f"{entity}.{column}: only on one side")
        for column in sorted(left_cols & right_cols):
            if str(left[column].dtype) != str(right[column].dtype):
                differences.append(f"{entity}.{column}: dtype differs")
            elif not left[column].equals(right[column]):
                differences.append(f"{entity}.{column}: values differ")
    return {"equal": not differences, "differences": differences[:50]}


def publish_manifest_impl(bucket: str, plan: dict) -> str:
    from policyengine_simulation_executor.artifact_store import ArtifactStore

    return ArtifactStore(bucket).write_manifest(build_manifest_payload(plan))


def run_precompute(
    bucket: str,
    *,
    force: bool,
    plan_artifacts: Any,
    build_dataset: Any,
    compute_baseline: Any,
    verify_determinism: Any,
    publish_manifest: Any,
    echo: Callable[[str], None] = print,
) -> str:
    """The local-entrypoint orchestration: plan, spawn misses, gate, publish.

    Takes the five Modal function handles as parameters (each offering
    ``.remote``/``.spawn``) so the flow is testable with plain fakes. The
    determinism gate runs only when baselines were actually computed (a
    no-op run must stay a no-op). Returns the manifest digest; the LAST
    echoed line is the ``MANIFEST_DIGEST=`` contract the deploy job parses.
    """
    echo(f"Planning against gs://{bucket} (force={force})")
    plan = plan_artifacts.remote(bucket)
    work = select_work(plan, force=force)
    echo(
        f"Plan: {len(plan['datasets'])} datasets ({len(work['datasets'])} to "
        f"build), {len(plan['baselines'])} baselines "
        f"({len(work['baselines'])} to compute)"
    )

    dataset_handles = [
        build_dataset.spawn(bucket, entry["year"], entry["path"])
        for entry in work["datasets"]
    ]
    for handle in dataset_handles:
        result = handle.get()
        echo(
            f"dataset year={result['year']} built in "
            f"{result['build_seconds']}s ({result['size_bytes']} bytes, "
            f"uploaded={result['uploaded']})"
        )

    baseline_handles = [
        compute_baseline.spawn(bucket, entry["year"], entry["group"], entry)
        for entry in work["baselines"]
    ]
    for handle in baseline_handles:
        result = handle.get()
        echo(
            f"baseline year={result['year']} id={result['simulation_id']} "
            f"outcome={result['outcome']} in {result['compute_seconds']}s "
            f"(uploaded={result['uploaded']})"
        )

    if work["baselines"]:
        probe = work["baselines"][0]
        echo(
            f"Determinism gate: recomputing year={probe['year']} group={probe['group']}"
        )
        verdict = verify_determinism.remote(
            bucket, probe["year"], probe["group"], probe
        )
        if not verdict["equal"]:
            raise SystemExit(
                "Determinism gate FAILED: " + "; ".join(verdict["differences"])
            )
        echo("Determinism gate passed")

    manifest_digest = publish_manifest.remote(bucket, plan)
    echo(manifest_digest_line(manifest_digest))
    return manifest_digest
