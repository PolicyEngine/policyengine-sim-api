"""Tests for the daily read that keeps the Stage 12 v2 manifest from expiring.

Modal expires a Dict entry after 7 days without a read or write. The
entrypoint reads the Stage 12 v2 manifest only for a Stage 12 request, and
Stage 12 is off in beta, so without a deployment for 7 days the manifest
would expire. A scheduled workflow, Stage 12's own and separate from the v1
routing jobs, reads every key of it once a day in each Modal environment a
deployment publishes it to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml
from test_deployment_assets_support import (
    DEPLOY_WORKFLOW,
    REPOSITORY_ROOT,
    REUSABLE_DEPLOY_WORKFLOW,
    WORKFLOWS_DIRECTORY,
    step_shell,
)
from workflow_script_support import FakeCommands, run_script

if TYPE_CHECKING:
    from pathlib import Path

REFRESH_WORKFLOW = WORKFLOWS_DIRECTORY / "stage12-v2-manifest-refresh.yml"
STAGE12_DEPLOY_WORKFLOW = WORKFLOWS_DIRECTORY / "stage12-deploy.yml"
REFRESH_SCRIPT = "stage12-v2-manifest-refresh.sh"
EXECUTOR_DIRECTORY = REPOSITORY_ROOT / "projects" / "policyengine-simulation-executor"
V1_ROUTING_CLI = "src.modal.utils.update_version_registry"
REFRESH_STEP = "Read every key of the v2 manifest"


def _workflow() -> dict:
    return yaml.safe_load(REFRESH_WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML reads the bare key `on` as True.
    return workflow.get("on", workflow.get(True))


def _refresh_job() -> dict:
    jobs = _workflow()["jobs"]
    assert list(jobs) == ["refresh"]
    return jobs["refresh"]


def _deployed_modal_environments() -> set[str]:
    """Return the Modal environments the deploy workflow's callers publish to."""
    environments = set()
    callers = []
    for workflow_path in sorted(WORKFLOWS_DIRECTORY.glob("*.yml")):
        jobs = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))["jobs"]
        for job in jobs.values():
            if not str(job.get("uses", "")).endswith(REUSABLE_DEPLOY_WORKFLOW.name):
                continue
            callers.append(workflow_path.name)
            arguments = job.get("with") or {}
            if arguments.get("deploy_stage12_v2"):
                environments.add(arguments["modal_environment"])
    # The scan must find the known callers, so that a renamed reusable
    # workflow cannot make it pass by finding nothing.
    assert {DEPLOY_WORKFLOW.name, STAGE12_DEPLOY_WORKFLOW.name} <= set(callers)
    return environments


def test_the_manifest_is_read_daily_and_on_request():
    triggers = _triggers(_workflow())

    assert set(triggers) == {"schedule", "workflow_dispatch"}
    (schedule,) = triggers["schedule"]
    minute, hour, day_of_month, month, day_of_week = schedule["cron"].split()
    # One run a day leaves six more before an unread entry expires.
    assert minute.isdigit()
    assert hour.isdigit()
    assert (day_of_month, month, day_of_week) == ("*", "*", "*")


def test_each_modal_environment_a_deployment_publishes_to_is_read():
    job = _refresh_job()

    assert job["strategy"]["fail-fast"] is False
    assert set(job["strategy"]["matrix"]["modal_environment"]) == (
        _deployed_modal_environments()
    )
    assert _deployed_modal_environments() == {"staging", "main"}


def test_the_read_never_waits_on_or_cancels_a_deployment():
    workflow = _workflow()

    # A queued run in simulation-deploy-main would cancel a pending deployment.
    assert workflow["concurrency"]["group"] != "simulation-deploy-main"
    assert workflow["permissions"] == {"contents": "read"}
    assert "environment" not in _refresh_job()


def test_the_read_step_runs_the_refresh_with_modal_access_only():
    steps = _refresh_job()["steps"]
    names = [step.get("name") for step in steps]
    refresh = steps[names.index(REFRESH_STEP)]
    install = steps[names.index("Install Stage 12 manifest publisher dependencies")]

    assert steps[0]["uses"].startswith("actions/checkout@")
    assert names.index("Install Stage 12 manifest publisher dependencies") < (
        names.index(REFRESH_STEP)
    )
    assert install["run"] == "uv sync --frozen"
    assert refresh["run"] == f"../../.github/scripts/{REFRESH_SCRIPT}"
    assert "--refresh" in step_shell(refresh)
    for step in (install, refresh):
        assert step["working-directory"] == "projects/policyengine-simulation-executor"
    assert refresh["env"] == {
        "MODAL_TOKEN_ID": "${{ secrets.MODAL_TOKEN_ID }}",
        "MODAL_TOKEN_SECRET": "${{ secrets.MODAL_TOKEN_SECRET }}",
        "MODAL_ENVIRONMENT": "${{ matrix.modal_environment }}",
    }
    assert "continue-on-error" not in refresh
    # Nothing skips the read, on a schedule or otherwise.
    assert "if" not in refresh
    assert "if" not in _refresh_job()
    assert "continue-on-error" not in _refresh_job()
    # A hung Modal call ends well before the next day's run.
    assert 0 < _refresh_job()["timeout-minutes"] <= 30


def test_the_refresh_shares_nothing_with_v1_routing():
    for step in _refresh_job()["steps"]:
        assert V1_ROUTING_CLI not in step_shell(step)


@pytest.fixture
def fakes(tmp_path: Path) -> FakeCommands:
    commands = FakeCommands.create(tmp_path)
    commands.add("uv", 'exit "${FAKE_UV_EXIT:-0}"\n')
    return commands


@pytest.mark.parametrize(("uv_exit", "script_exit"), [("0", 0), ("1", 1)])
def test_the_refresh_script_reads_the_manifest_and_fails_when_the_read_fails(
    fakes, uv_exit, script_exit
):
    result = run_script(
        REFRESH_SCRIPT,
        env=fakes.env(MODAL_ENVIRONMENT="staging", FAKE_UV_EXIT=uv_exit),
        cwd=EXECUTOR_DIRECTORY,
    )

    assert result.returncode == script_exit, result.stderr
    assert fakes.calls() == [
        [
            "uv",
            "run",
            "--frozen",
            "python",
            "-m",
            "src.modal.utils.update_v2_version_manifest",
            "--environment",
            "staging",
            "--manifest-name",
            "simulation-api-v2-version-manifest",
            "--refresh",
        ]
    ]
