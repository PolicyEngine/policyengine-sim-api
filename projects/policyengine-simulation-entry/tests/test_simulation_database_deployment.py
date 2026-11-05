"""Deployment wiring for the simulation service's database.

The entrypoint receives its role's URL as a pinned Cloud Run secret on every
deploy, whatever the Stage 12 setting, and the candidate smoke requires it to
reach the database with every table privilege its role needs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_deployment_assets_support import (
    CLOUD_RUN_DEPLOY_SCRIPT,
    REUSABLE_DEPLOY_WORKFLOW,
    SMOKE_SCRIPT,
    cloud_run_deploy_environment,
    smoke_test_env,
    workflow_jobs,
    write_fake_smoke_curl,
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
        "SIMULATION_SERVICE_DATABASE_URL=simulation-staging-service-database-url:3"
        in _secrets_argument(environment).split(",")
    )
    runtime_environment = json.loads(
        Path(environment["GCLOUD_ENV_FILE_CAPTURE"]).read_text(encoding="utf-8")
    )
    assert "SIMULATION_SERVICE_DATABASE_URL" not in runtime_environment


@pytest.mark.parametrize(
    "secret_name",
    ["", "not a secret name", "projects/x/secrets/name/versions/1"],
)
def test_the_entrypoint_deploy_requires_a_valid_database_url_secret(
    tmp_path, secret_name
):
    result, environment = _run_deploy(
        tmp_path, SIMULATION_SERVICE_DATABASE_URL_SECRET_NAME=secret_name
    )

    assert result.returncode != 0
    assert "SIMULATION_SERVICE_DATABASE_URL_SECRET_NAME" in result.stderr or (
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

    assert deploy_step["env"]["SIMULATION_SERVICE_DATABASE_URL_SECRET_NAME"] == (
        "${{ vars.SIMULATION_SERVICE_DATABASE_URL_SECRET_NAME }}"
    )


# --- The candidate smoke --------------------------------------------------


def test_the_candidate_smoke_requires_access_to_the_database():
    integration = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["integration"]
    smoke = next(
        step
        for step in integration["steps"]
        if step.get("name") == "Verify the complete deployed request path"
    )

    assert smoke["env"]["REQUIRE_SIMULATION_DATABASE_ACCESS"] == "true"


def _ready_body(*, reachable=True, has_required_privileges=True):
    return json.dumps(
        {
            "status": "ready",
            "simulation_database": {
                "reachable": reachable,
                "has_required_privileges": (
                    has_required_privileges if reachable else None
                ),
            },
        }
    )


def _run_smoke(tmp_path, ready_body, *, require):
    state_file, revisions_file = write_fake_smoke_curl(tmp_path, ["revision-1"])
    environment = smoke_test_env(tmp_path, state_file, revisions_file) | {
        "FAKE_CURL_READY": ready_body,
    }
    if require is not None:
        environment["REQUIRE_SIMULATION_DATABASE_ACCESS"] = require
    return subprocess.run(
        ["bash", SMOKE_SCRIPT, "https://candidate.example.test", "revision-1"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_the_smoke_accepts_a_reached_database_with_every_privilege(tmp_path):
    result = _run_smoke(tmp_path, _ready_body(), require="true")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "ready_body",
    [
        _ready_body(reachable=False),
        _ready_body(has_required_privileges=False),
        json.dumps({"status": "ready"}),
    ],
    ids=["unreachable", "missing-privilege", "unconfigured"],
)
def test_the_smoke_rejects_a_database_the_service_cannot_use(tmp_path, ready_body):
    result = _run_smoke(tmp_path, ready_body, require="true")

    assert result.returncode == 1
    assert "simulation database" in result.stderr


@pytest.mark.parametrize(
    "ready_body",
    [_ready_body(reachable=False), _ready_body(has_required_privileges=False)],
    ids=["unreachable", "missing-privilege"],
)
def test_the_smoke_points_to_the_cloud_run_logs_for_the_cause(tmp_path, ready_body):
    """/ready only says yes or no; the service logs what is wrong."""
    result = _run_smoke(tmp_path, ready_body, require="true")

    assert result.returncode == 1
    assert "Cloud Run logs" in result.stderr
    assert "simulation_database_unreachable" in result.stderr
    assert "simulation_database_missing_privileges" in result.stderr


def _run_smoke_with_ready_bodies(tmp_path, bodies):
    bodies_file = tmp_path / "ready-bodies"
    bodies_file.write_text("\n".join(bodies) + "\n", encoding="utf-8")
    state_file, revisions_file = write_fake_smoke_curl(tmp_path, ["revision-1"])
    environment = smoke_test_env(tmp_path, state_file, revisions_file) | {
        "FAKE_CURL_READY_BODIES": str(bodies_file),
        "REQUIRE_SIMULATION_DATABASE_ACCESS": "true",
        "SIMULATION_DATABASE_READY_MAX_ATTEMPTS": "3",
    }
    result = subprocess.run(
        ["bash", SMOKE_SCRIPT, "https://candidate.example.test", "revision-1"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    ready_requests = int(Path(f"{bodies_file}.count").read_text(encoding="utf-8"))
    return result, ready_requests


def test_the_smoke_waits_for_a_database_a_cold_instance_has_not_reached(tmp_path):
    result, ready_requests = _run_smoke_with_ready_bodies(
        tmp_path,
        [_ready_body(reachable=False), _ready_body(reachable=False), _ready_body()],
    )

    assert result.returncode == 0, result.stderr
    assert ready_requests == 3


def test_the_smoke_stops_waiting_for_an_unreachable_database(tmp_path):
    result, ready_requests = _run_smoke_with_ready_bodies(
        tmp_path, [_ready_body(reachable=False)]
    )

    assert result.returncode == 1
    assert ready_requests == 3
    assert "simulation database" in result.stderr


def test_the_smoke_does_not_wait_for_privileges_the_role_lacks(tmp_path):
    result, ready_requests = _run_smoke_with_ready_bodies(
        tmp_path,
        [_ready_body(has_required_privileges=False), _ready_body()],
    )

    assert result.returncode == 1
    assert ready_requests == 1


def test_stable_and_rollback_smokes_ignore_the_database(tmp_path):
    result = _run_smoke(tmp_path, _ready_body(reachable=False), require=None)

    assert result.returncode == 0, result.stderr


def test_the_smoke_refuses_an_unknown_database_requirement(tmp_path):
    result = _run_smoke(tmp_path, _ready_body(), require="yes")

    assert result.returncode == 2
    assert "REQUIRE_SIMULATION_DATABASE_ACCESS" in result.stderr
