"""Scheduled Modal functions that fail or restart runs and household jobs.

The stuck-run sweep runs every ten minutes. It lists the Public API's report
runs, simulation runs and household jobs that have been pending or running
for at least 15 minutes, counted from a run's first claim or, while it is
pending, from its creation, and from a household job's creation. It asks
Modal for the status of each recorded call and marks a run or job failed
once its call has ended, and fails a pending one that has no call recorded,
because nothing will run it. It also fails any still unfinished 24 hours
after it was created or started, longer than any Modal call of it can run,
whatever Modal reports. Its result counts runs and household jobs together.

The restart check runs every two minutes. It starts once more the call of
each run or household job created 3 to 12 minutes ago that is still pending
with no call recorded, as when the simulation service stopped between
creating it and starting its call; the simulation service's own restart code
(``policyengine_simulation_entry.run_restart_check``) rebuilds the call from
the stored rows and records it. A run it can't start again is failed with
``run_not_started``, as the sweep would fail it at 15 minutes. Its telemetry
counts each restart and how it ended.

Both connect as the Modal worker role, from the same Modal secret the v2
coordinator, workers and household functions use. This application is not
versioned with the .py bundle: one copy runs per Modal environment. Its
image bakes in, at deployment, the Modal environment the restart check starts
calls in and the v2 workers' telemetry settings.

Each deployment from ``main`` deploys it to its Modal environment after
every check, in ``publish_stage12_v2_default``, once that job has moved the
Stage 12 v2 default (when the release is newer) and checked that a reader
resolves it (``simulation-deploy.reusable.yml``); if this deploy fails,
that job puts the previous default back. Modal refuses the deployment
while the Modal secret is missing from that environment.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

from policyengine_simulation_executor.stage12_runtime.report_run_access import (
    MODAL_WORKER_DATABASE_SECRET_NAME,
    MODAL_WORKER_DATABASE_URL_ENV,
)
from policyengine_simulation_observability.observability import (
    modal_image_environment,
)
from src.modal.ca_certificates import GRPC_CA_BUNDLE_ENV, OPERATING_SYSTEM_CA_PACKAGE

import modal

APP_NAME = "policyengine-simulation-run-sweep"
# The Modal environment the restart check reads the v2 manifest from and
# starts calls in, baked into the image from the deploy step's environment.
V2_MODAL_ENVIRONMENT_ENV = "SIMULATION_V2_MODAL_ENVIRONMENT"
_UV_PROJECT_DIR = str(Path(__file__).resolve().parents[2]) if modal.is_local() else "."

app = modal.App(APP_NAME)
modal_worker_database_secret = modal.Secret.from_name(
    MODAL_WORKER_DATABASE_SECRET_NAME,
    required_keys=[MODAL_WORKER_DATABASE_URL_ENV],
)
sweep_image = (
    modal.Image.debian_slim(python_version="3.13")
    # The same CA store and locked packages as the v2 images, without the
    # bundle's data, which the sweep never reads.
    .apt_install(OPERATING_SYSTEM_CA_PACKAGE)
    .env(GRPC_CA_BUNDLE_ENV)
    .env(
        {
            **modal_image_environment(),
            V2_MODAL_ENVIRONMENT_ENV: os.getenv(V2_MODAL_ENVIRONMENT_ENV, ""),
        }
    )
    .uv_sync(
        uv_project_dir=_UV_PROJECT_DIR,
        frozen=True,
        extra_options="--only-group modal-simulation-image",
        # The repository's uv release, from .uv-version.
        uv_version="0.13.6",
    )
    .add_local_python_source(
        # This module imports src.modal, so the container needs it too.
        "src.modal",
        "policyengine_simulation_executor",
        "policyengine_simulation_observability",
        "policyengine_simulation_contract",
        "policyengine_stage12_persistence",
        # The restart check runs the simulation service's restart code.
        "policyengine_simulation_persistence",
        "policyengine_simulation_entry",
        copy=True,
    )
)


@app.function(
    image=sweep_image,
    schedule=modal.Cron("*/10 * * * *"),
    cpu=0.25,
    memory=512,
    timeout=600,
    retries=0,
    max_containers=1,
    secrets=[modal_worker_database_secret],
)
def sweep_stuck_runs() -> dict:
    """Fail the runs and household jobs nothing will finish; count them and why."""
    from policyengine_simulation_executor.stage12_runtime import (  # noqa: PLC0415  # Modal function body: loaded in the container, not at deploy time
        report_run_access,
        run_sweep,
    )

    summary = run_sweep.sweep_stuck_runs(report_run_access.report_run_store())
    return asdict(summary)


@app.function(
    image=sweep_image,
    schedule=modal.Cron("*/2 * * * *"),
    cpu=0.25,
    memory=512,
    timeout=300,
    retries=0,
    max_containers=1,
    secrets=[modal_worker_database_secret],
)
def restart_unstarted_runs() -> dict:
    """Start again the runs and household jobs whose call never started; count them."""
    from policyengine_simulation_entry import (  # noqa: PLC0415  # Modal function body: loaded in the container, not at deploy time
        run_restart_check,
    )
    from policyengine_simulation_observability import (  # noqa: PLC0415  # Modal function body: loaded in the container, not at deploy time
        observability,
    )

    modal_environment = os.environ[V2_MODAL_ENVIRONMENT_ENV]
    runtime = observability.init_process_observability(
        service_name="policyengine-simulation-run-restart",
        service_role="v2_run_restart_check",
        platform="modal",
        environment=modal_environment,
    )
    try:
        return run_restart_check.run_restart_check(
            database_url=os.environ[MODAL_WORKER_DATABASE_URL_ENV],
            modal_environment=modal_environment,
            runtime=runtime,
        )
    finally:
        runtime.shutdown()
