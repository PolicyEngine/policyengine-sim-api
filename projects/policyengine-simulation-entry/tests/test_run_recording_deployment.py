"""Deployment settings the background recording loop needs.

The loop runs between requests, so Cloud Run must keep the instance's CPU
allocated outside them; and it asks Modal about calls, so every deploy mounts
sim's own Modal token, whether or not it also deploys the Stage 12 path.
"""

from __future__ import annotations

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
    arguments_capture = Path(environment["GCLOUD_ARGUMENTS_CAPTURE"])
    arguments = (
        arguments_capture.read_text(encoding="utf-8").splitlines()
        if arguments_capture.exists()
        else []
    )
    return result, arguments


@pytest.mark.parametrize("deploy_stage12", ["true", "false"])
def test_every_deploy_keeps_cpu_allocated_between_requests(
    tmp_path, deploy_stage12
) -> None:
    result, arguments = _run_deploy(tmp_path, DEPLOY_STAGE12_V2=deploy_stage12)

    assert result.returncode == 0, result.stderr
    assert "--no-cpu-throttling" in arguments
    assert "--cpu-throttling" not in arguments


@pytest.mark.parametrize("deploy_stage12", ["true", "false"])
def test_every_deploy_mounts_sims_own_modal_token(tmp_path, deploy_stage12) -> None:
    result, arguments = _run_deploy(tmp_path, DEPLOY_STAGE12_V2=deploy_stage12)

    assert result.returncode == 0, result.stderr
    secrets = arguments[arguments.index("--set-secrets") + 1].split(",")
    assert "MODAL_TOKEN_ID=modal-token-id:3" in secrets
    assert "MODAL_TOKEN_SECRET=modal-token-secret:3" in secrets
    assert len(secrets) == len(set(secrets))


@pytest.mark.parametrize(
    "variable", ["MODAL_TOKEN_ID_SECRET_NAME", "MODAL_TOKEN_SECRET_SECRET_NAME"]
)
def test_a_deploy_without_the_modal_token_stops(tmp_path, variable) -> None:
    result, arguments = _run_deploy(
        tmp_path, DEPLOY_STAGE12_V2="false", **{variable: ""}
    )

    assert result.returncode != 0
    assert variable in result.stderr
    assert arguments == []


def test_the_deploy_step_always_passes_the_modal_token_secret_names() -> None:
    deploy_entrypoint = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["deploy_entrypoint"]
    environment = next(
        step["env"]
        for step in deploy_entrypoint["steps"]
        if "deploy-cloud-run-simulation-entry.sh" in step.get("run", "")
    )

    assert environment["MODAL_TOKEN_ID_SECRET_NAME"] == (
        "${{ vars.MODAL_TOKEN_ID_SECRET_NAME }}"
    )
    assert environment["MODAL_TOKEN_SECRET_SECRET_NAME"] == (
        "${{ vars.MODAL_TOKEN_SECRET_SECRET_NAME }}"
    )
