# pyright: reportAttributeAccessIssue=false
"""Ephemeral Modal application that builds the independent Stage 12 cache."""

from __future__ import annotations

import shlex
from pathlib import Path

from policyengine_simulation_executor.hf_access_validation import (
    HF_MODAL_SECRET_NAME,
    HF_RUNTIME_ENV_NAME,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle

import modal

STAGE12_DATA_DIR = "/opt/policyengine/stage12-data"
_UV_PROJECT_DIR = str(Path(__file__).resolve().parents[2]) if modal.is_local() else "."
RESOLVED_BUNDLE = load_stage12_bundle()

app = modal.App("policyengine-stage12-cache-precompute")
gcp_secret = modal.Secret.from_name("stage12-evaluation-gcp-credentials")
# The read-only Hugging Face credential the other simulation apps mount. The
# US cache reads only the public policyengine/populace-us dataset repository.
hf_secret = modal.Secret.from_name(
    HF_MODAL_SECRET_NAME,
    required_keys=[HF_RUNTIME_ENV_NAME],
)
worker_secrets = [gcp_secret, hf_secret]


def _bundle_install_command() -> str:
    parts = [
        "uvx",
        "--from",
        RESOLVED_BUNDLE.bundle.policyengine_requirement,
        "policyengine",
        "bundle",
        "install",
        RESOLVED_BUNDLE.bundle.policyengine_version,
        "--venv",
        "/.uv/.venv",
        "--country",
        "us",
        "--data-dir",
        STAGE12_DATA_DIR,
        "--yes",
    ]
    return " ".join(shlex.quote(part) for part in parts)


precompute_image = (
    modal.Image.debian_slim(python_version="3.13")
    .uv_sync(
        uv_project_dir=_UV_PROJECT_DIR,
        frozen=True,
        extra_options="--only-group modal-simulation-image",
    )
    .run_commands(_bundle_install_command(), secrets=[hf_secret])
    .env({"POLICYENGINE_DATA_FOLDER": STAGE12_DATA_DIR})
    .add_local_python_source(
        "src.modal",
        "policyengine_simulation_executor",
        "policyengine_simulation_observability",
        "policyengine_simulation_contract",
        "policyengine_stage12_persistence",
        copy=True,
    )
)


@app.function(
    image=precompute_image,
    cpu=4.0,
    memory=16384,
    timeout=1800,
    secrets=worker_secrets,
)
def plan_artifacts(bucket: str) -> dict:
    from policyengine_simulation_executor.stage12_cache.precompute import (
        plan_artifacts_impl,
    )

    return plan_artifacts_impl(bucket).model_dump()


@app.function(
    image=precompute_image,
    cpu=8.0,
    memory=65536,
    timeout=7200,
    secrets=worker_secrets,
)
def build_dataset(bucket: str, expected: dict) -> dict:
    from policyengine_simulation_executor.stage12_cache.models import DatasetPlanEntry
    from policyengine_simulation_executor.stage12_cache.precompute import (
        build_dataset_impl,
    )

    return build_dataset_impl(
        bucket, DatasetPlanEntry.model_validate(expected)
    ).model_dump()


@app.function(
    image=precompute_image,
    cpu=8.0,
    memory=32768,
    timeout=3600,
    secrets=worker_secrets,
)
def compute_baseline(bucket: str, expected: dict) -> dict:
    from policyengine_simulation_executor.stage12_cache.models import BaselinePlanEntry
    from policyengine_simulation_executor.stage12_cache.precompute import (
        compute_baseline_impl,
    )

    return compute_baseline_impl(
        bucket, BaselinePlanEntry.model_validate(expected)
    ).model_dump()


@app.function(
    image=precompute_image,
    cpu=8.0,
    memory=32768,
    timeout=3600,
    secrets=worker_secrets,
)
def verify_determinism(bucket: str, plan: dict, expected: dict) -> dict:
    from policyengine_simulation_executor.stage12_cache.models import (
        BaselinePlanEntry,
        PrecomputePlan,
    )
    from policyengine_simulation_executor.stage12_cache.precompute import (
        verify_determinism_impl,
    )

    return verify_determinism_impl(
        bucket,
        PrecomputePlan.model_validate(plan),
        BaselinePlanEntry.model_validate(expected),
    ).model_dump()


@app.function(
    image=precompute_image,
    cpu=2.0,
    memory=8192,
    timeout=600,
    secrets=worker_secrets,
)
def publish_manifest(bucket: str, plan: dict) -> str:
    from policyengine_simulation_executor.stage12_cache.models import PrecomputePlan
    from policyengine_simulation_executor.stage12_cache.precompute import (
        publish_manifest_impl,
    )

    return publish_manifest_impl(bucket, PrecomputePlan.model_validate(plan))


@app.local_entrypoint()
def main():
    from policyengine_simulation_executor.stage12_cache.precompute import (
        run_precompute,
    )
    from policyengine_simulation_executor.stage12_cache.store import (
        resolve_cache_bucket,
    )

    run_precompute(
        resolve_cache_bucket(),
        plan_artifacts=plan_artifacts,
        build_dataset=build_dataset,
        compute_baseline=compute_baseline,
        verify_determinism=verify_determinism,
        publish_manifest=publish_manifest,
    )
