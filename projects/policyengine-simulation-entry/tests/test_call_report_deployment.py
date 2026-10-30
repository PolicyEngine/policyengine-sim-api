"""Deploy settings for the route on which Modal reports that a call ended.

The entrypoint accepts reports only from the call-report function of one
Modal workspace and environment. The workspace ID comes from the `beta` and
`prod` GitHub environment variable `MODAL_WORKSPACE_ID`; the environment is
the deployment's own Modal environment.
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
from workflow_script_support import FakeCommands, github_output, run_script


def _deploy(tmp_path, **overrides):
    environment = cloud_run_deploy_environment(tmp_path) | overrides
    result = subprocess.run(
        [CLOUD_RUN_DEPLOY_SCRIPT],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    capture = Path(environment["GCLOUD_ENV_FILE_CAPTURE"])
    runtime = (
        json.loads(capture.read_text(encoding="utf-8")) if capture.exists() else {}
    )
    return result, runtime


@pytest.mark.parametrize(
    ("app_environment", "modal_environment"),
    [("staging", "staging"), ("production", "main")],
)
def test_the_entrypoint_expects_reports_from_its_own_modal_environment(
    tmp_path, app_environment, modal_environment
) -> None:
    result, runtime = _deploy(
        tmp_path,
        APP_ENVIRONMENT=app_environment,
        MODAL_ENVIRONMENT=modal_environment,
        MODAL_WORKSPACE_ID_VALUE="ac-workspace0001",
    )

    assert result.returncode == 0, result.stderr
    assert runtime["SIMULATION_CALL_REPORT_MODAL_WORKSPACE_ID"] == "ac-workspace0001"
    assert runtime["SIMULATION_CALL_REPORT_MODAL_ENVIRONMENT"] == modal_environment


@pytest.mark.parametrize("workspace_id", ["", "workspace0001", "ac-", "ac-a b"])
def test_a_deploy_without_a_modal_workspace_id_stops(tmp_path, workspace_id) -> None:
    result, runtime = _deploy(tmp_path, MODAL_WORKSPACE_ID_VALUE=workspace_id)

    assert result.returncode != 0
    assert "MODAL_WORKSPACE_ID_VALUE" in result.stderr
    assert runtime == {}


def test_the_deploy_step_passes_the_modal_workspace_id() -> None:
    deploy_entrypoint = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["deploy_entrypoint"]
    environment = next(
        step["env"]
        for step in deploy_entrypoint["steps"]
        if "deploy-cloud-run-simulation-entry.sh" in step.get("run", "")
    )

    assert environment["MODAL_WORKSPACE_ID_VALUE"] == "${{ vars.MODAL_WORKSPACE_ID }}"


URL_SCRIPT = "simulation-entry-main-url.sh"


def _find_url(tmp_path, service_url, **overrides):
    fakes = FakeCommands.create(tmp_path)
    fakes.add("gcloud", f"printf '%s\\n' '{service_url}'\n")
    output = tmp_path / "github-output"
    settings = {
        "PROJECT_ID": "policyengine",
        "REGION": "us-central1",
        "SERVICE": "policyengine-simulation-entry",
        "GITHUB_OUTPUT": str(output),
    }
    result = run_script(URL_SCRIPT, env=fakes.env(**(settings | overrides)))
    return result, fakes, github_output(output)


def test_the_modal_functions_report_to_the_entrypoints_main_url(tmp_path) -> None:
    url = "https://policyengine-simulation-entry-abc-uc.a.run.app"

    result, fakes, outputs = _find_url(tmp_path, url)

    assert result.returncode == 0, result.stderr
    assert outputs == {"url": url}
    (describe,) = fakes.calls("gcloud")
    assert describe[1:5] == [
        "run",
        "services",
        "describe",
        "policyengine-simulation-entry",
    ]
    assert "--format=value(status.url)" in describe


@pytest.mark.parametrize("service_url", ["", "http://insecure.example", "not a url"])
def test_a_main_url_that_is_not_https_stops_the_deploy(tmp_path, service_url) -> None:
    result, _, outputs = _find_url(tmp_path, service_url)

    assert result.returncode != 0
    assert outputs == {}


def test_the_v2_deploy_passes_the_main_url_to_the_modal_app() -> None:
    job = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["deploy_stage12_v2"]
    steps = job["steps"]
    find = next(
        index for index, step in enumerate(steps) if URL_SCRIPT in step.get("run", "")
    )
    deploy = next(
        index
        for index, step in enumerate(steps)
        if "src/modal/v2_app.py" in step.get("run", "")
    )

    assert find < deploy
    assert steps[find]["id"] == "entry_url"
    assert steps[find]["env"]["SERVICE"] == "${{ inputs.entrypoint_service }}"
    assert steps[deploy]["env"]["SIMULATION_ENTRY_URL"] == (
        "${{ steps.entry_url.outputs.url }}"
    )
