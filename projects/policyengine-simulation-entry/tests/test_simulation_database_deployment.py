"""Deployment wiring for the simulation service's database.

The entrypoint receives its role's URL as a pinned Cloud Run secret on every
deploy, whatever the Stage 12 setting.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_deployment_assets_cloud_run import cloud_run_deploy_environment
from test_deployment_assets_support import (
    CLOUD_RUN_DEPLOY_SCRIPT,
    REUSABLE_DEPLOY_WORKFLOW,
    workflow_jobs,
)


def _run_deploy(tmp_path, **overrides):
    environment = cloud_run_deploy_environment(tmp_path) | overrides
    result = subprocess.run(
        [CLOUD_RUN_DEPLOY_SCRIPT],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result, environment


def _secrets_argument(environment) -> str:
    arguments = (
        Path(environment["GCLOUD_ARGUMENTS_CAPTURE"])
        .read_text(encoding="utf-8")
        .splitlines()
    )
    return arguments[arguments.index("--set-secrets") + 1]


@pytest.mark.parametrize("deploy_stage12", ["true", "false"])
def test_the_entrypoint_receives_the_database_url_as_a_pinned_secret(
    tmp_path, deploy_stage12
):
    result, environment = _run_deploy(tmp_path, DEPLOY_STAGE12_V2=deploy_stage12)

    assert result.returncode == 0, result.stderr
    assert (
        "SIMULATION_DATABASE_URL=simulation-staging-service-database-url:3"
        in _secrets_argument(environment).split(",")
    )
    runtime_environment = json.loads(
        Path(environment["GCLOUD_ENV_FILE_CAPTURE"]).read_text(encoding="utf-8")
    )
    assert "SIMULATION_DATABASE_URL" not in runtime_environment


@pytest.mark.parametrize(
    "secret_name",
    ["", "not a secret name", "projects/x/secrets/name/versions/1"],
)
def test_the_entrypoint_deploy_requires_a_valid_database_url_secret(
    tmp_path, secret_name
):
    result, environment = _run_deploy(
        tmp_path, SIMULATION_DATABASE_URL_SECRET_NAME=secret_name
    )

    assert result.returncode != 0
    assert "SIMULATION_DATABASE_URL_SECRET_NAME" in result.stderr or (
        "Secret Manager reference" in result.stderr
    )
    assert not Path(environment["GCLOUD_ARGUMENTS_CAPTURE"]).exists()


def test_the_deploy_step_passes_the_database_url_secret_name():
    deploy_entrypoint = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["deploy_entrypoint"]
    deploy_step = next(
        step
        for step in deploy_entrypoint["steps"]
        if step.get("name") == "Deploy Cloud Run entrypoint candidate"
    )

    assert deploy_step["env"]["SIMULATION_DATABASE_URL_SECRET_NAME"] == (
        "${{ vars.SIMULATION_DATABASE_URL_SECRET_NAME }}"
    )
