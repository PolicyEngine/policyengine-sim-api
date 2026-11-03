"""A scheduled Modal function that fails runs left running after their call ended.

Every ten minutes it lists the Public API's report runs and simulation runs
that have been running under a recorded Modal call for at least ten minutes,
asks Modal for each call's status, and marks a run failed once its call has
ended. It also fails any run still running 24 hours after it started, longer
than any Modal call of it can run, whatever Modal reports. It connects as
the Modal worker role, from the same Modal secret the v2 coordinator and
workers use. This application is not versioned with the
.py bundle: one copy runs per Modal environment.

Deploy it, once per Modal environment, from this project's directory:

    uv run --frozen modal deploy --env=staging src/modal/run_sweep_app.py
    uv run --frozen modal deploy --env=main src/modal/run_sweep_app.py
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from policyengine_simulation_executor.stage12_runtime.report_run_access import (
    MODAL_WORKER_DATABASE_SECRET_NAME,
    MODAL_WORKER_DATABASE_URL_ENV,
)
from src.modal.ca_certificates import GRPC_CA_BUNDLE_ENV, OPERATING_SYSTEM_CA_PACKAGE

import modal

APP_NAME = "policyengine-simulation-run-sweep"
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
    """Fail the runs whose Modal call ended; return how many were checked and why."""
    from policyengine_simulation_executor.stage12_runtime import (  # noqa: PLC0415  # Modal function body: loaded in the container, not at deploy time
        report_run_access,
        run_sweep,
    )

    summary = run_sweep.sweep_stuck_runs(report_run_access.report_run_store())
    return asdict(summary)
