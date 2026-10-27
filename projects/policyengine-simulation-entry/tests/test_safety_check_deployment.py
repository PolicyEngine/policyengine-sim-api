"""Deploying the safety-check job from the revision that serves traffic.

The job runs only the image of the promoted (or restored) entrypoint
revision, so a rejected candidate never records anything. The deploy copies
that revision's image, service account, database URL and Modal token, and
keeps a Cloud Scheduler entry that starts the job every five minutes.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from policyengine_simulation_entry.run_recording import SAFETY_CHECK_INTERVAL
from test_deployment_assets_support import (
    REUSABLE_DEPLOY_WORKFLOW,
    ROLLBACK_WORKFLOW,
    workflow_jobs,
)
from workflow_script_support import FakeCommands, run_script

SCRIPT = "deploy-cloud-run-simulation-safety-check.sh"
SERVICE = "policyengine-simulation-entry"
REVISION = "policyengine-simulation-entry-p-123"
IMAGE = (
    "us-central1-docker.pkg.dev/policyengine/policyengine-simulation-entry/"
    "policyengine-simulation-entry@sha256:" + "a" * 64
)
RUNTIME_ACCOUNT = "sim-entry-prod-runtime@policyengine.iam.gserviceaccount.com"
SCHEDULER_ACCOUNT = "sim-safety-check-prod@policyengine.iam.gserviceaccount.com"
JOB = f"{SERVICE}-safety-check"


def _secret(name, secret, version):
    return {
        "name": name,
        "valueFrom": {"secretKeyRef": {"name": secret, "key": version}},
    }


def _revision(*, service=SERVICE, environment=None):
    return {
        "apiVersion": "serving.knative.dev/v1",
        "kind": "Revision",
        "metadata": {
            "name": REVISION,
            "labels": {"serving.knative.dev/service": service},
        },
        "spec": {
            "serviceAccountName": RUNTIME_ACCOUNT,
            "containers": [
                {
                    "image": "us-central1-docker.pkg.dev/policyengine/x/y:sha",
                    "env": environment
                    if environment is not None
                    else [
                        {"name": "APP_ENVIRONMENT", "value": "production"},
                        {"name": "OLD_GATEWAY_URL", "value": "https://legacy.example"},
                        {"name": "OBSERVABILITY_SERVICE_NAMESPACE", "value": "sim"},
                        {"name": "CLOUD_RUN_REGION", "value": "us-central1"},
                        _secret(
                            "SIMULATION_DATABASE_URL", "simulation-runtime-url", "4"
                        ),
                        _secret("MODAL_TOKEN_ID", "modal-token-id", "3"),
                        _secret("MODAL_TOKEN_SECRET", "modal-token-secret", "3"),
                        _secret(
                            "OLD_GATEWAY_AUTH_CLIENT_SECRET", "old-client-secret", "2"
                        ),
                    ],
                }
            ],
        },
        "status": {"imageDigest": IMAGE},
    }


def _fakes(tmp_path, revision, *, schedule_exists):
    fakes = FakeCommands.create(tmp_path)
    revision_file = tmp_path / "revision.json"
    revision_file.write_text(json.dumps(revision), encoding="utf-8")
    environment_capture = tmp_path / "job-environment.json"
    fakes.add(
        "gcloud",
        f"""
        case "$1 $2 $3" in
          "run revisions describe") cat "{revision_file}" ;;
          "run jobs deploy")
            while (($#)); do
              if [[ "$1" == "--env-vars-file" ]]; then
                cp "$2" "{environment_capture}"
              fi
              shift
            done
            ;;
          "scheduler jobs describe") {"exit 0" if schedule_exists else "exit 1"} ;;
          *) ;;
        esac
        """,
    )
    return fakes, environment_capture


def _deploy(tmp_path, revision=None, *, schedule_exists=False, **overrides):
    fakes, environment_capture = _fakes(
        tmp_path, revision or _revision(), schedule_exists=schedule_exists
    )
    settings = {
        "PROJECT_ID": "policyengine",
        "REGION": "us-central1",
        "SERVICE": SERVICE,
        "SAFETY_CHECK_REVISION": REVISION,
        "SAFETY_CHECK_SCHEDULER_SERVICE_ACCOUNT": SCHEDULER_ACCOUNT,
        "RUNNER_TEMP": str(tmp_path),
    }
    result = run_script(SCRIPT, env=fakes.env(**(settings | overrides)))
    return result, fakes, environment_capture


def _call(fakes, *prefix):
    calls = [
        call
        for call in fakes.calls("gcloud")
        if call[1 : 1 + len(prefix)] == list(prefix)
    ]
    assert len(calls) == 1, calls
    return calls[0]


def _flag(call, name):
    return call[call.index(name) + 1]


def test_the_job_runs_the_serving_revisions_exact_image_and_identity(tmp_path) -> None:
    result, fakes, _ = _deploy(tmp_path)

    assert result.returncode == 0, result.stderr
    deploy = _call(fakes, "run", "jobs", "deploy")
    assert deploy[4] == JOB
    assert _flag(deploy, "--image") == IMAGE
    assert _flag(deploy, "--service-account") == RUNTIME_ACCOUNT
    assert _flag(deploy, "--command") == "/opt/venv/bin/python"
    assert "--args=-m,policyengine_simulation_entry.safety_check" in deploy
    assert _flag(deploy, "--tasks") == "1"
    assert _flag(deploy, "--max-retries") == "0"
    assert _flag(deploy, "--project") == "policyengine"
    assert _flag(deploy, "--region") == "us-central1"


def test_the_job_gets_only_the_secrets_it_reads_at_the_revisions_versions(
    tmp_path,
) -> None:
    result, fakes, environment_capture = _deploy(tmp_path)

    assert result.returncode == 0, result.stderr
    secrets = _flag(_call(fakes, "run", "jobs", "deploy"), "--set-secrets").split(",")
    assert sorted(secrets) == [
        "MODAL_TOKEN_ID=modal-token-id:3",
        "MODAL_TOKEN_SECRET=modal-token-secret:3",
        "SIMULATION_DATABASE_URL=simulation-runtime-url:4",
    ]
    environment = json.loads(environment_capture.read_text(encoding="utf-8"))
    assert environment == {
        "APP_ENVIRONMENT": "production",
        "OBSERVABILITY_SERVICE_NAMESPACE": "sim",
        "CLOUD_RUN_REGION": "us-central1",
    }


@pytest.mark.parametrize("schedule_exists", [False, True])
def test_the_schedule_starts_the_job_every_interval(tmp_path, schedule_exists) -> None:
    result, fakes, _ = _deploy(tmp_path, schedule_exists=schedule_exists)

    assert result.returncode == 0, result.stderr
    action = "update" if schedule_exists else "create"
    schedule = _call(fakes, "scheduler", "jobs", action, "http")
    assert schedule[5] == JOB
    assert _flag(schedule, "--location") == "us-central1"
    assert _flag(schedule, "--schedule") == "*/5 * * * *"
    assert _flag(schedule, "--uri") == (
        "https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/"
        f"namespaces/policyengine/jobs/{JOB}:run"
    )
    assert _flag(schedule, "--http-method") == "POST"
    assert _flag(schedule, "--oauth-service-account-email") == SCHEDULER_ACCOUNT


def test_the_schedule_matches_the_safety_check_interval() -> None:
    assert timedelta(minutes=5) == SAFETY_CHECK_INTERVAL


def test_a_revision_of_another_service_is_refused(tmp_path) -> None:
    result, fakes, _ = _deploy(tmp_path, _revision(service="another-service"))

    assert result.returncode != 0
    assert "another-service" in result.stderr
    assert [call[1:3] for call in fakes.calls("gcloud")] == [["run", "revisions"]]


def test_a_revision_without_the_simulation_database_pauses_the_schedule(
    tmp_path,
) -> None:
    """An older revision restored by a rollback has no safety check."""
    revision = _revision(
        environment=[{"name": "APP_ENVIRONMENT", "value": "production"}]
    )

    result, fakes, _ = _deploy(tmp_path, revision, schedule_exists=True)

    assert result.returncode == 0, result.stderr
    assert [call[1:4] for call in fakes.calls("gcloud")] == [
        ["run", "revisions", "describe"],
        ["scheduler", "jobs", "describe"],
        ["scheduler", "jobs", "pause"],
    ]


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("SAFETY_CHECK_SCHEDULER_SERVICE_ACCOUNT", ""),
        ("SAFETY_CHECK_SCHEDULER_SERVICE_ACCOUNT", "not-an-account"),
        ("SAFETY_CHECK_REVISION", ""),
        ("SAFETY_CHECK_REVISION", "Bad Revision"),
        ("SERVICE", ""),
    ],
)
def test_incomplete_settings_stop_before_any_change(tmp_path, variable, value) -> None:
    result, fakes, _ = _deploy(tmp_path, **{variable: value})

    assert result.returncode != 0
    assert variable in result.stderr
    assert fakes.calls("gcloud") == []


def _steps(job):
    return job["steps"]


def _step_named(steps, name):
    return next(index for index, step in enumerate(steps) if step.get("name") == name)


def test_promotion_points_the_job_at_the_revision_after_it_is_verified() -> None:
    steps = _steps(workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["promote_entrypoint"])
    promote = _step_named(steps, "Promote exact tested revision")
    safety = _step_named(steps, "Point the safety check at the promoted revision")
    restore = _step_named(steps, "Restore previous revision after failed verification")
    restore_safety = _step_named(
        steps, "Point the safety check back at the previous revision"
    )

    verification = [
        index
        for index, step in enumerate(steps)
        if index > promote and step.get("name", "").startswith("Verify")
    ]
    assert verification
    assert max(verification) < safety < restore < restore_safety
    assert SCRIPT in steps[safety]["run"]
    assert steps[safety]["env"]["SAFETY_CHECK_REVISION"] == "${{ env.TARGET_REVISION }}"
    assert steps[safety]["env"]["SAFETY_CHECK_SCHEDULER_SERVICE_ACCOUNT"] == (
        "${{ vars.SIMULATION_SAFETY_CHECK_SCHEDULER_SERVICE_ACCOUNT }}"
    )
    assert "steps.safety_check.outcome == 'failure'" in steps[restore_safety]["if"]
    assert steps[restore_safety]["env"]["SAFETY_CHECK_REVISION"] == (
        "${{ env.PREVIOUS_REVISION }}"
    )


def test_no_candidate_deploy_touches_the_job() -> None:
    jobs = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)
    for name, job in jobs.items():
        runs = " ".join(step.get("run", "") for step in job.get("steps", []))
        if SCRIPT in runs:
            assert name == "promote_entrypoint"


def test_a_rollback_points_the_job_at_the_restored_revision() -> None:
    steps = _steps(workflow_jobs(ROLLBACK_WORKFLOW)["rollback"])
    restore = _step_named(steps, "Restore entrypoint traffic")
    safety = _step_named(steps, "Point the safety check at the restored revision")

    assert restore < safety
    assert steps[safety]["if"] == "${{ inputs.entrypoint_revision != '' }}"
    assert steps[safety]["env"]["SAFETY_CHECK_REVISION"] == (
        "${{ inputs.entrypoint_revision }}"
    )
