"""Deployment wiring for the household calculation routes.

The entrypoint calls the v2 workers' household functions on Modal, so every
deploy, whatever the Stage 12 setting, gives it the Modal token as pinned
Cloud Run secrets and the v2 manifest's Modal environment and name.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_deployment_assets_support import (
    CLOUD_RUN_DEPLOY_SCRIPT,
    REUSABLE_DEPLOY_WORKFLOW,
    cloud_run_deploy_environment,
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


def _secrets(environment) -> list[str]:
    arguments = (
        Path(environment["GCLOUD_ARGUMENTS_CAPTURE"])
        .read_text(encoding="utf-8")
        .splitlines()
    )
    return arguments[arguments.index("--set-secrets") + 1].split(",")


def _runtime_environment(environment) -> dict[str, str]:
    return json.loads(
        Path(environment["GCLOUD_ENV_FILE_CAPTURE"]).read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("deploy_stage12", ["true", "false"])
def test_every_deploy_mounts_the_modal_token_once(tmp_path, deploy_stage12):
    result, environment = _run_deploy(tmp_path, DEPLOY_STAGE12_V2=deploy_stage12)

    assert result.returncode == 0, result.stderr
    secrets = _secrets(environment)
    assert secrets.count("MODAL_TOKEN_ID=modal-token-id:3") == 1
    assert secrets.count("MODAL_TOKEN_SECRET=modal-token-secret:3") == 1
    assert [secret.partition("=")[0] for secret in secrets].count("MODAL_TOKEN_ID") == 1
    runtime_environment = _runtime_environment(environment)
    assert "MODAL_TOKEN_ID" not in runtime_environment
    assert "MODAL_TOKEN_SECRET" not in runtime_environment


@pytest.mark.parametrize(
    ("app_environment", "modal_environment", "deploy_stage12"),
    [
        ("staging", "staging", "true"),
        ("staging", "staging", "false"),
        ("production", "main", "true"),
        ("production", "main", "false"),
    ],
)
def test_every_deploy_names_the_v2_manifest_and_its_modal_environment(
    tmp_path, app_environment, modal_environment, deploy_stage12
):
    result, environment = _run_deploy(
        tmp_path,
        APP_ENVIRONMENT=app_environment,
        MODAL_ENVIRONMENT=modal_environment,
        DEPLOY_STAGE12_V2=deploy_stage12,
    )

    assert result.returncode == 0, result.stderr
    runtime_environment = _runtime_environment(environment)
    assert runtime_environment["SIMULATION_V2_MODAL_ENVIRONMENT"] == modal_environment
    assert runtime_environment["SIMULATION_V2_MANIFEST_NAME"] == (
        "simulation-api-v2-version-manifest"
    )


def test_without_stage12_no_stage12_setting_is_deployed(tmp_path):
    """The household settings are the household path's own."""
    result, environment = _run_deploy(tmp_path, DEPLOY_STAGE12_V2="false")

    assert result.returncode == 0, result.stderr
    runtime_environment = _runtime_environment(environment)
    assert runtime_environment["STAGE12_ENABLED"] == "0"
    assert not {
        "STAGE12_V2_MANIFEST_NAME",
        "STAGE12_V2_MANIFEST_ENVIRONMENT",
        "STAGE12_ARTIFACT_BUCKET",
    } & set(runtime_environment)
    assert not any(
        secret.startswith("STAGE12_DATABASE_URL=") for secret in _secrets(environment)
    )


@pytest.mark.parametrize(
    "secret_name_variable",
    ["MODAL_TOKEN_ID_SECRET_NAME", "MODAL_TOKEN_SECRET_SECRET_NAME"],
)
@pytest.mark.parametrize("deploy_stage12", ["true", "false"])
def test_a_deploy_without_the_modal_token_secret_names_stops(
    tmp_path, secret_name_variable, deploy_stage12
):
    result, environment = _run_deploy(
        tmp_path, DEPLOY_STAGE12_V2=deploy_stage12, **{secret_name_variable: ""}
    )

    assert result.returncode != 0
    assert f"{secret_name_variable} must be set" in result.stderr
    assert not Path(environment["GCLOUD_ARGUMENTS_CAPTURE"]).exists()


def test_the_deploy_step_passes_the_modal_token_secret_names():
    deploy_entrypoint = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["deploy_entrypoint"]
    deploy_step = next(
        step
        for step in deploy_entrypoint["steps"]
        if step.get("name") == "Deploy Cloud Run entrypoint candidate"
    )

    assert deploy_step["env"]["MODAL_TOKEN_ID_SECRET_NAME"] == (
        "${{ vars.MODAL_TOKEN_ID_SECRET_NAME }}"
    )
    assert deploy_step["env"]["MODAL_TOKEN_SECRET_SECRET_NAME"] == (
        "${{ vars.MODAL_TOKEN_SECRET_SECRET_NAME }}"
    )
    assert deploy_step["env"]["MODAL_ENVIRONMENT"] == "${{ inputs.modal_environment }}"
