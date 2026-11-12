"""Deployment wiring for the economy report and simulation routes.

Every deploy gives the service the bucket that keeps v2 simulation output
files for good (the environment variable ``V2_SIMULATION_OUTPUT_BUCKET``),
so a report reuses only a succeeded run whose file is in it. The service
compares output URIs with it and never reads the bucket.
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


@pytest.mark.parametrize("deploy_stage12", ["true", "false"])
def test_every_deploy_names_the_v2_output_bucket(tmp_path, deploy_stage12):
    result, environment = _run_deploy(
        tmp_path,
        DEPLOY_STAGE12_V2=deploy_stage12,
        V2_SIMULATION_OUTPUT_BUCKET_VALUE="policyengine-v2-outputs-staging",
    )

    assert result.returncode == 0, result.stderr
    runtime_environment = json.loads(
        Path(environment["GCLOUD_ENV_FILE_CAPTURE"]).read_text(encoding="utf-8")
    )
    assert runtime_environment["V2_SIMULATION_OUTPUT_BUCKET"] == (
        "policyengine-v2-outputs-staging"
    )


@pytest.mark.parametrize(
    ("bucket", "message"),
    [
        ("", "V2_SIMULATION_OUTPUT_BUCKET_VALUE must be set"),
        (
            "gs://policyengine-v2-outputs",
            "V2_SIMULATION_OUTPUT_BUCKET_VALUE is invalid",
        ),
        ("Policyengine", "V2_SIMULATION_OUTPUT_BUCKET_VALUE is invalid"),
    ],
)
def test_a_deploy_without_a_valid_output_bucket_stops(tmp_path, bucket, message):
    result, environment = _run_deploy(
        tmp_path, V2_SIMULATION_OUTPUT_BUCKET_VALUE=bucket
    )

    assert result.returncode != 0
    assert message in result.stderr
    assert not Path(environment["GCLOUD_ARGUMENTS_CAPTURE"]).exists()


def test_the_deploy_step_passes_the_environments_output_bucket():
    deploy_entrypoint = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["deploy_entrypoint"]
    deploy_step = next(
        step
        for step in deploy_entrypoint["steps"]
        if step.get("name") == "Deploy Cloud Run entrypoint candidate"
    )

    assert deploy_step["env"]["V2_SIMULATION_OUTPUT_BUCKET_VALUE"] == (
        "${{ vars.V2_SIMULATION_OUTPUT_BUCKET }}"
    )
