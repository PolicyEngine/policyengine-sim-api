"""Ephemeral precompute app: fill the artifact store for the installed bundle.

Modal plumbing only — every function body lives in
``policyengine_simulation_executor.precompute`` (the deployed app's split:
thin decorated wrappers, library implementations). Run as a `modal run`
(no deploy):

    uv run modal run --env=staging src/modal/precompute_app.py
    uv run modal run --env=staging src/modal/precompute_app.py --force

Wave 1 builds the single-year US datasets (one container per year), wave 2
computes the 20 per-cohort national baselines per year (one container per
cohort, sized like the production ``run_simulation_segment`` workers). See
the library module for the idempotency, ``--force``, and determinism-gate
semantics.
"""

from __future__ import annotations

import modal

from src.modal.app import (
    build_runtime_simulation_image,
    data_secret,
    gcp_secret,
    hf_secret,
)

app = modal.App("policyengine-simulation-precompute")

precompute_image = build_runtime_simulation_image().add_local_python_source(
    "src.modal",
    "policyengine_simulation_executor",
    "policyengine_simulation_observability",
    "policyengine_simulation_contract",
    copy=True,
)

_worker_secrets = [gcp_secret, data_secret, hf_secret]


# Wrapper signatures use plain dicts: they sit on Modal's serialization
# boundary. Each validates into the strict precompute_models schema on the
# way in and dumps on the way out, so planner/worker shape drift fails
# loudly at the edge.


@app.function(
    image=precompute_image,
    cpu=4.0,
    memory=16384,
    timeout=1800,
    secrets=_worker_secrets,
)
def plan_artifacts(bucket: str) -> dict:
    from policyengine_simulation_executor.precompute import plan_artifacts_impl

    return plan_artifacts_impl(bucket).model_dump()


@app.function(
    image=precompute_image,
    cpu=8.0,
    memory=65536,
    timeout=2 * 60 * 60,
    secrets=_worker_secrets,
)
def build_dataset(bucket: str, expected: dict) -> dict:
    from policyengine_simulation_executor.precompute import build_dataset_impl
    from policyengine_simulation_executor.precompute_models import DatasetPlanEntry

    return build_dataset_impl(
        bucket, DatasetPlanEntry.model_validate(expected)
    ).model_dump()


@app.function(
    image=precompute_image,
    cpu=8.0,
    memory=32768,
    timeout=3600,
    secrets=_worker_secrets,
)
def compute_baseline(bucket: str, expected: dict) -> dict:
    from policyengine_simulation_executor.precompute import compute_baseline_impl
    from policyengine_simulation_executor.precompute_models import BaselinePlanEntry

    return compute_baseline_impl(
        bucket, BaselinePlanEntry.model_validate(expected)
    ).model_dump()


@app.function(
    image=precompute_image,
    cpu=8.0,
    memory=32768,
    timeout=3600,
    secrets=_worker_secrets,
)
def verify_determinism(bucket: str, expected: dict) -> dict:
    from policyengine_simulation_executor.precompute import verify_determinism_impl
    from policyengine_simulation_executor.precompute_models import BaselinePlanEntry

    return verify_determinism_impl(
        bucket, BaselinePlanEntry.model_validate(expected)
    ).model_dump()


@app.function(
    image=precompute_image,
    cpu=2.0,
    memory=8192,
    timeout=600,
    secrets=_worker_secrets,
)
def publish_manifest(bucket: str, plan: dict) -> str:
    from policyengine_simulation_executor.precompute import publish_manifest_impl
    from policyengine_simulation_executor.precompute_models import PrecomputePlan

    return publish_manifest_impl(bucket, PrecomputePlan.model_validate(plan))


@app.local_entrypoint()
def main(force: bool = False):
    from policyengine_simulation_executor.artifact_store import resolve_bucket_name
    from policyengine_simulation_executor.precompute import run_precompute

    run_precompute(
        resolve_bucket_name(),
        force=force,
        plan_artifacts=plan_artifacts,
        build_dataset=build_dataset,
        compute_baseline=compute_baseline,
        verify_determinism=verify_determinism,
        publish_manifest=publish_manifest,
    )
