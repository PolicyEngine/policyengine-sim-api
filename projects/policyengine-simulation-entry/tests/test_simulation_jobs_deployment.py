"""Deployment wiring for background collection of simulation job results.

The queue and the invoker service account come from GitHub environment
variables; the collection target is the stable service URL the deploy job
reads before deploying; the Modal environment is the deployment's own.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_deployment_assets import (
    CLOUD_RUN_DEPLOY_SCRIPT,
    REUSABLE_DEPLOY_WORKFLOW,
    _cloud_run_deploy_environment,
    _workflow_jobs,
)

QUEUE = "projects/policyengine-staging/locations/us-central1/queues/simulation-jobs"
INVOKER = "simulation-jobs-invoker@policyengine-staging.iam.gserviceaccount.com"
STABLE_URL = "https://policyengine-simulation-entry-staging-abc-uc.a.run.app"


def _run_deploy(tmp_path, **overrides):
    environment = _cloud_run_deploy_environment(tmp_path) | overrides
    result = subprocess.run(
        [CLOUD_RUN_DEPLOY_SCRIPT],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result, environment


def test_the_entrypoint_receives_the_reconciliation_settings(tmp_path):
    result, environment = _run_deploy(tmp_path)

    assert result.returncode == 0, result.stderr
    runtime_environment = json.loads(
        Path(environment["GCLOUD_ENV_FILE_CAPTURE"]).read_text(encoding="utf-8")
    )
    assert runtime_environment["RECONCILIATION_QUEUE"] == QUEUE
    assert runtime_environment["RECONCILIATION_INVOKER_SERVICE_ACCOUNT"] == INVOKER
    assert runtime_environment["RECONCILIATION_TARGET_URL"] == STABLE_URL
    assert runtime_environment["SIMULATION_MODAL_ENVIRONMENT"] == "staging"


@pytest.mark.parametrize(
    "variable",
    [
        "RECONCILIATION_QUEUE_VALUE",
        "RECONCILIATION_INVOKER_SERVICE_ACCOUNT_VALUE",
        "ENTRYPOINT_STABLE_URL",
    ],
)
def test_the_deploy_requires_every_reconciliation_setting(tmp_path, variable):
    result, _ = _run_deploy(tmp_path, **{variable: ""})

    assert result.returncode != 0
    assert variable in result.stderr


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("RECONCILIATION_QUEUE_VALUE", "simulation-jobs"),
        ("RECONCILIATION_QUEUE_VALUE", f"{QUEUE}/tasks"),
        ("RECONCILIATION_INVOKER_SERVICE_ACCOUNT_VALUE", "invoker@example.com"),
        ("ENTRYPOINT_STABLE_URL", "http://entry.a.run.app"),
        ("ENTRYPOINT_STABLE_URL", f"{STABLE_URL}/path"),
    ],
)
def test_the_deploy_rejects_malformed_reconciliation_settings(
    tmp_path, variable, value
):
    result, _ = _run_deploy(tmp_path, **{variable: value})

    assert result.returncode != 0
    assert variable in result.stderr


def test_the_deploy_step_passes_the_settings_and_the_stable_url():
    deploy_entrypoint = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["deploy_entrypoint"]
    steps = [step.get("name") for step in deploy_entrypoint["steps"]]
    deploy_step = next(
        step
        for step in deploy_entrypoint["steps"]
        if step.get("name") == "Deploy Cloud Run entrypoint candidate"
    )

    assert deploy_step["env"]["RECONCILIATION_QUEUE_VALUE"] == (
        "${{ vars.RECONCILIATION_QUEUE }}"
    )
    assert deploy_step["env"]["RECONCILIATION_INVOKER_SERVICE_ACCOUNT_VALUE"] == (
        "${{ vars.RECONCILIATION_INVOKER_SERVICE_ACCOUNT }}"
    )
    assert deploy_step["env"]["ENTRYPOINT_STABLE_URL"] == (
        "${{ steps.previous.outputs.stable_url }}"
    )
    assert steps.index("Capture stable service state before deployment") < (
        steps.index("Deploy Cloud Run entrypoint candidate")
    )
