"""Deployment wiring for the Simulation Entrypoint's own database.

The migration job applies the chain before the entrypoint candidate deploys,
with the migrator URL in that job alone; the entrypoint receives the runtime
URL as a pinned Cloud Run secret; and the candidate smoke requires the
database to be at the image's revision.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_deployment_assets import (
    CLOUD_RUN_DEPLOY_SCRIPT,
    REUSABLE_DEPLOY_WORKFLOW,
    SMOKE_SCRIPT,
    _cloud_run_deploy_environment,
    _gh_eval,
    _smoke_test_env,
    _workflow_jobs,
    _write_fake_smoke_curl,
)
from workflow_script_support import WORKFLOWS_DIR, FakeCommands, run_script

MIGRATION_SCRIPT = "migrate-simulation-database.sh"
MIGRATION_JOB = "migrate_simulation_database"
MIGRATION_SECRET = "SIMULATION_MIGRATION_DATABASE_URL"
FAKE_MIGRATION_URL = "postgresql://simulation_migrator:secret@db.example/postgres"


@pytest.fixture
def fakes(tmp_path: Path) -> FakeCommands:
    return FakeCommands.create(tmp_path)


# --- The migration job ----------------------------------------------------


def test_the_migration_job_runs_before_the_entrypoint_deploys():
    jobs = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)
    migration = jobs[MIGRATION_JOB]
    deploy_entrypoint = jobs["deploy_entrypoint"]

    assert migration["needs"] == "prepare"
    assert migration["environment"] == "${{ inputs.release_environment }}"
    assert MIGRATION_JOB in deploy_entrypoint["needs"]
    context = {
        "inputs": {"deploy_stage12_v2": True},
        "needs": {
            "prepare": {"result": "success"},
            "configure_stage12_v2": {"result": "success"},
            MIGRATION_JOB: {"result": "success"},
        },
    }
    assert _gh_eval(deploy_entrypoint["if"], context, job_if=True)
    for failed_result in ("failure", "skipped", "cancelled"):
        context["needs"][MIGRATION_JOB]["result"] = failed_result
        assert not _gh_eval(deploy_entrypoint["if"], context, job_if=True)


def test_the_migration_job_applies_the_chain_with_the_migrator_url_alone():
    migration = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)[MIGRATION_JOB]
    steps = {step.get("name"): step for step in migration["steps"]}
    install = steps["Install migration dependencies"]
    apply_step = steps["Apply simulation schema migrations"]

    assert install["working-directory"] == "libs/policyengine-simulation-persistence"
    assert install["run"] == "uv sync --frozen --extra migrations"
    assert apply_step["working-directory"] == "libs/policyengine-simulation-persistence"
    assert apply_step["run"] == f"../../.github/scripts/{MIGRATION_SCRIPT}"
    assert apply_step["env"] == {
        MIGRATION_SECRET: "${{ secrets.SIMULATION_MIGRATION_DATABASE_URL }}",
        "SIMULATION_MIGRATION_MODE": (
            "${{ github.ref == 'refs/heads/main' && 'upgrade' || 'verify' }}"
        ),
    }
    assert "env" not in migration
    assert "permissions" not in migration


def test_only_the_migration_step_receives_the_migrator_url():
    carriers = []
    for workflow in sorted(WORKFLOWS_DIR.glob("*.yml")):
        for job_name, job in _workflow_jobs(workflow).items():
            if MIGRATION_SECRET in json.dumps(job.get("env", {})):
                carriers.append((workflow.name, job_name, "job env"))
            carriers.extend(
                (workflow.name, job_name, step.get("name"))
                for step in job.get("steps", [])
                if MIGRATION_SECRET in json.dumps(step)
            )

    assert carriers == [
        (
            "simulation-deploy.reusable.yml",
            MIGRATION_JOB,
            "Apply simulation schema migrations",
        )
    ]


UV_ALEMBIC = ["uv", "run", "--frozen", "--extra", "migrations", "alembic", "-c",
              "../../migrations/alembic.ini"]  # fmt: skip


def _run_migration_script(fakes, **environment):
    values = {
        "SIMULATION_MIGRATION_DATABASE_URL": FAKE_MIGRATION_URL,
        "SIMULATION_MIGRATION_MODE": "upgrade",
        **environment,
    }
    return run_script(MIGRATION_SCRIPT, env=fakes.env(**values))


def test_the_migration_script_upgrades_and_then_requires_the_head(fakes):
    fakes.add("uv")

    result = _run_migration_script(fakes)

    assert result.returncode == 0, result.stderr
    assert fakes.calls("uv") == [
        [*UV_ALEMBIC, "upgrade", "head"],
        [*UV_ALEMBIC, "current", "--check-heads"],
    ]
    assert "secret" not in result.stdout + result.stderr


def test_verify_mode_only_requires_the_database_at_the_head(fakes):
    fakes.add("uv")

    result = _run_migration_script(fakes, SIMULATION_MIGRATION_MODE="verify")

    assert result.returncode == 0, result.stderr
    assert fakes.calls("uv") == [[*UV_ALEMBIC, "current", "--check-heads"]]


@pytest.mark.parametrize("mode", ["", "downgrade", "UPGRADE"])
def test_the_migration_script_refuses_an_unknown_mode(fakes, mode):
    fakes.add("uv")

    result = _run_migration_script(fakes, SIMULATION_MIGRATION_MODE=mode)

    assert result.returncode != 0
    assert "SIMULATION_MIGRATION_MODE" in result.stderr
    assert fakes.calls("uv") == []


def test_the_migration_script_refuses_to_run_without_the_migrator_url(fakes):
    fakes.add("uv")

    result = _run_migration_script(fakes, SIMULATION_MIGRATION_DATABASE_URL="")

    assert result.returncode != 0
    assert MIGRATION_SECRET in result.stderr
    assert fakes.calls("uv") == []


def test_a_failed_upgrade_fails_the_job_before_the_head_check(fakes):
    fakes.add("uv", 'if [[ " $* " == *" upgrade "* ]]; then exit 3; fi\n')

    result = _run_migration_script(fakes)

    assert result.returncode == 3
    assert len(fakes.calls("uv")) == 1


def test_only_the_default_branch_applies_migrations():
    migration = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)[MIGRATION_JOB]
    steps = {step.get("name"): step for step in migration["steps"]}
    mode = steps["Apply simulation schema migrations"]["env"][
        "SIMULATION_MIGRATION_MODE"
    ]

    # A deployment dispatched from another branch must not apply that branch's
    # revisions; it only requires the database to be at its chain's head.
    assert _gh_eval(mode, {"github": {"ref": "refs/heads/main"}}) == "upgrade"
    assert _gh_eval(mode, {"github": {"ref": "refs/heads/feature"}}) == "verify"


# --- The entrypoint's runtime URL -----------------------------------------


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


def _secrets_argument(environment) -> str:
    arguments = (
        Path(environment["GCLOUD_ARGUMENTS_CAPTURE"])
        .read_text(encoding="utf-8")
        .splitlines()
    )
    return arguments[arguments.index("--set-secrets") + 1]


@pytest.mark.parametrize("deploy_stage12", ["true", "false"])
def test_the_entrypoint_receives_the_runtime_url_as_a_pinned_secret(
    tmp_path, deploy_stage12
):
    result, environment = _run_deploy(tmp_path, DEPLOY_STAGE12_V2=deploy_stage12)

    assert result.returncode == 0, result.stderr
    assert (
        "SIMULATION_DATABASE_URL=simulation-staging-runtime-database-url:3"
        in _secrets_argument(environment).split(",")
    )
    runtime_environment = json.loads(
        Path(environment["GCLOUD_ENV_FILE_CAPTURE"]).read_text(encoding="utf-8")
    )
    assert "SIMULATION_DATABASE_URL" not in runtime_environment
    assert MIGRATION_SECRET not in runtime_environment


@pytest.mark.parametrize(
    "secret_name",
    ["", "not a secret name", "projects/x/secrets/name/versions/1"],
)
def test_the_entrypoint_deploy_requires_a_valid_runtime_url_secret(
    tmp_path, secret_name
):
    result, _ = _run_deploy(tmp_path, SIMULATION_DATABASE_URL_SECRET_NAME=secret_name)

    assert result.returncode != 0
    assert "SIMULATION_DATABASE_URL_SECRET_NAME" in result.stderr or (
        "Secret Manager reference" in result.stderr
    )


def test_the_deploy_step_passes_the_runtime_secret_name():
    deploy_entrypoint = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["deploy_entrypoint"]
    deploy_step = next(
        step
        for step in deploy_entrypoint["steps"]
        if step.get("name") == "Deploy Cloud Run entrypoint candidate"
    )

    assert deploy_step["env"]["SIMULATION_DATABASE_URL_SECRET_NAME"] == (
        "${{ vars.SIMULATION_DATABASE_URL_SECRET_NAME }}"
    )


# --- The candidate smoke --------------------------------------------------


def test_the_candidate_smoke_requires_the_current_database_revision():
    integration = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["integration"]
    smoke = next(
        step
        for step in integration["steps"]
        if step.get("name") == "Verify the complete deployed request path"
    )

    assert smoke["env"]["REQUIRE_CURRENT_SIMULATION_DATABASE_REVISION"] == "true"


def _ready_body(revision, expected_revision="0001"):
    return json.dumps(
        {
            "status": "ready",
            "simulation_database": {
                "reachable": revision is not None,
                "revision": revision,
                "expected_revision": expected_revision,
            },
        }
    )


def _run_smoke(tmp_path, ready_body, *, require):
    state_file, revisions_file = _write_fake_smoke_curl(tmp_path, ["revision-1"])
    environment = _smoke_test_env(tmp_path, state_file, revisions_file) | {
        "FAKE_CURL_READY": ready_body,
    }
    if require is not None:
        environment["REQUIRE_CURRENT_SIMULATION_DATABASE_REVISION"] = require
    return subprocess.run(
        ["bash", SMOKE_SCRIPT, "https://candidate.example.test", "revision-1"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_the_smoke_accepts_a_database_at_the_expected_revision(tmp_path):
    result = _run_smoke(tmp_path, _ready_body("0001"), require="true")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "ready_body",
    [
        _ready_body(None),
        _ready_body("0001", expected_revision="0002"),
        json.dumps({"status": "ready"}),
    ],
    ids=["unreachable", "behind", "unconfigured"],
)
def test_the_smoke_rejects_a_database_not_at_the_expected_revision(
    tmp_path, ready_body
):
    result = _run_smoke(tmp_path, ready_body, require="true")

    assert result.returncode == 1
    assert "simulation database" in result.stderr


def _run_smoke_with_ready_bodies(tmp_path, bodies):
    bodies_file = tmp_path / "ready-bodies"
    bodies_file.write_text("\n".join(bodies) + "\n", encoding="utf-8")
    state_file, revisions_file = _write_fake_smoke_curl(tmp_path, ["revision-1"])
    environment = _smoke_test_env(tmp_path, state_file, revisions_file) | {
        "FAKE_CURL_READY_BODIES": str(bodies_file),
        "REQUIRE_CURRENT_SIMULATION_DATABASE_REVISION": "true",
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
        tmp_path, [_ready_body(None), _ready_body(None), _ready_body("0001")]
    )

    assert result.returncode == 0, result.stderr
    assert ready_requests == 3


def test_the_smoke_stops_waiting_for_an_unreachable_database(tmp_path):
    result, ready_requests = _run_smoke_with_ready_bodies(
        tmp_path, [_ready_body(None)]
    )

    assert result.returncode == 1
    assert ready_requests == 3
    assert "simulation database" in result.stderr


def test_the_smoke_does_not_wait_for_a_database_at_another_revision(tmp_path):
    result, ready_requests = _run_smoke_with_ready_bodies(
        tmp_path, [_ready_body("0001", expected_revision="0002"), _ready_body("0002")]
    )

    assert result.returncode == 1
    assert ready_requests == 1


def test_stable_and_rollback_smokes_ignore_the_database_revision(tmp_path):
    result = _run_smoke(tmp_path, _ready_body(None), require=None)

    assert result.returncode == 0, result.stderr


def test_the_smoke_refuses_an_unknown_database_requirement(tmp_path):
    result = _run_smoke(tmp_path, _ready_body("0001"), require="yes")

    assert result.returncode == 2
    assert "REQUIRE_CURRENT_SIMULATION_DATABASE_REVISION" in result.stderr
