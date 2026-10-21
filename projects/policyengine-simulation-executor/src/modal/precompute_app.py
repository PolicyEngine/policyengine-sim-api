"""Ephemeral precompute app: fill the artifact store for the installed bundle.

Modal plumbing only — every function body lives in
``policyengine_simulation_executor.precompute`` (the deployed app's split:
thin decorated wrappers, library implementations). Run as a `modal run`
(no deploy):

    uv run modal run --env=staging src/modal/precompute_app.py
    uv run modal run --env=staging src/modal/precompute_app.py --force

Wave 1 builds the single-year US datasets (one container per year), wave 2
computes the 20 per-cohort national baselines per year (one container per
cohort, sized like the production ``run_simulation_segment`` workers).
Alongside both waves, one container per year and dataset filters the
regional dataset that state regions load into one year file per state. See
the library module for the idempotency, ``--force``, and determinism-gate
semantics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from src.modal.app import (
    build_runtime_simulation_image,
    gcp_secret,
    hf_secret,
)

import modal

if TYPE_CHECKING:
    from policyengine_simulation_executor.precompute_models import RemoteFunction

app = modal.App("policyengine-simulation-precompute")

precompute_image = build_runtime_simulation_image().add_local_python_source(
    "src.modal",
    "policyengine_simulation_executor",
    "policyengine_simulation_observability",
    "policyengine_simulation_contract",
    copy=True,
)

_worker_secrets = [gcp_secret, hf_secret]


# Wrapper signatures use plain dicts: they sit on Modal's serialization
# boundary. Every structured value validates into the strict
# precompute_models schema on the way in and dumps back to a dict on the
# way out (bare strings — the bucket, publish_manifest's digest — cross
# as-is), so planner/worker shape drift fails loudly at the edge.


@app.function(
    image=precompute_image,
    cpu=4.0,
    memory=16384,
    timeout=1800,
    secrets=_worker_secrets,
)
def plan_artifacts(bucket: str) -> dict:
    """Return the dumped ``PrecomputePlan``: expected artifacts and store presence."""
    from policyengine_simulation_executor.precompute import (  # noqa: PLC0415  # Modal function body: loaded in the precompute worker, not at deploy time
        plan_artifacts_impl,
    )

    return plan_artifacts_impl(bucket).model_dump()


@app.function(
    image=precompute_image,
    cpu=8.0,
    memory=65536,
    timeout=2 * 60 * 60,
    secrets=_worker_secrets,
)
def build_dataset(bucket: str, expected: dict) -> dict:
    """Build and upload one single-year US dataset (wave 1).

    Raises if the dataset identity resolved in this container differs from the
    planned one, so planner/worker drift cannot upload under the wrong key.
    """
    from policyengine_simulation_executor.precompute import (  # noqa: PLC0415  # Modal function body: loaded in the precompute worker, not at deploy time
        build_dataset_impl,
    )
    from policyengine_simulation_executor.precompute_models import (  # noqa: PLC0415  # Modal function body: loaded in the precompute worker, not at deploy time
        DatasetPlanEntry,
    )

    return build_dataset_impl(
        bucket, DatasetPlanEntry.model_validate(expected)
    ).model_dump()


@app.function(
    image=precompute_image,
    cpu=8.0,
    # Preparing the regional dataset's full-country year file has used about
    # 51 GB; the 51 filtered copies need room on top of that.
    memory=98304,
    timeout=2 * 60 * 60,
    secrets=_worker_secrets,
)
def build_state_datasets(bucket: str, year: int, expected: list[dict]) -> list[dict]:
    """Build and upload one year's per-state datasets.

    Raises if a state's key resolved in this container differs from the
    planned one, so planner/worker drift cannot upload under the wrong key.
    """
    from policyengine_simulation_executor.precompute import (  # noqa: PLC0415  # Modal function body: loaded in the precompute worker, not at deploy time
        build_state_datasets_impl,
    )
    from policyengine_simulation_executor.precompute_models import (  # noqa: PLC0415  # Modal function body: loaded in the precompute worker, not at deploy time
        StateDatasetPlanEntry,
    )

    entries = [StateDatasetPlanEntry.model_validate(entry) for entry in expected]
    return [
        result.model_dump()
        for result in build_state_datasets_impl(bucket, year, entries)
    ]


@app.function(
    image=precompute_image,
    cpu=8.0,
    memory=32768,
    timeout=3600,
    secrets=_worker_secrets,
)
def compute_baseline(bucket: str, expected: dict) -> dict:
    """Compute and upload one cohort's national baseline output (wave 2)."""
    from policyengine_simulation_executor.precompute import (  # noqa: PLC0415  # Modal function body: loaded in the precompute worker, not at deploy time
        compute_baseline_impl,
    )
    from policyengine_simulation_executor.precompute_models import (  # noqa: PLC0415  # Modal function body: loaded in the precompute worker, not at deploy time
        BaselinePlanEntry,
    )

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
    """Recompute one stored cohort baseline afresh and report columns that differ."""
    from policyengine_simulation_executor.precompute import (  # noqa: PLC0415  # Modal function body: loaded in the precompute worker, not at deploy time
        verify_determinism_impl,
    )
    from policyengine_simulation_executor.precompute_models import (  # noqa: PLC0415  # Modal function body: loaded in the precompute worker, not at deploy time
        BaselinePlanEntry,
    )

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
    """Write the deploy manifest for ``plan`` to the store and return its digest."""
    from policyengine_simulation_executor.precompute import (  # noqa: PLC0415  # Modal function body: loaded in the precompute worker, not at deploy time
        publish_manifest_impl,
    )
    from policyengine_simulation_executor.precompute_models import (  # noqa: PLC0415  # Modal function body: loaded in the precompute worker, not at deploy time
        PrecomputePlan,
    )

    return publish_manifest_impl(bucket, PrecomputePlan.model_validate(plan))


@app.local_entrypoint()
def main(*, force: bool = False) -> None:
    """Fill the store for the installed bundle; ``--force`` recomputes stored ones."""
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # local entrypoint: loaded only when the precompute runs
        resolve_bucket_name,
    )
    from policyengine_simulation_executor.precompute import (  # noqa: PLC0415  # local entrypoint: loaded only when the precompute runs
        PrecomputeFunctions,
        run_precompute,
    )

    run_precompute(
        resolve_bucket_name(),
        force=force,
        # Pyright doesn't resolve `modal` here to the Modal package (it reports
        # `modal.App` as unknown), so it types each decorated function as the
        # plain function it wraps. At run time each is a Modal function.
        functions=PrecomputeFunctions(
            plan_artifacts=cast("RemoteFunction", plan_artifacts),
            build_dataset=cast("RemoteFunction", build_dataset),
            build_state_datasets=cast("RemoteFunction", build_state_datasets),
            compute_baseline=cast("RemoteFunction", compute_baseline),
            verify_determinism=cast("RemoteFunction", verify_determinism),
            publish_manifest=cast("RemoteFunction", publish_manifest),
        ),
    )
