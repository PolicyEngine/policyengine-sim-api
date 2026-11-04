"""Tests for the scripts the manual rollback workflow runs.

Each step of simulation-rollback.yml that restores routing, reads the restored
release, resolves the stable service, restores entrypoint traffic or verifies
the result runs one of these scripts. Each test runs a script with fake uv,
gcloud or sibling scripts (jq is real) and asserts on the exact calls,
$GITHUB_OUTPUT and exit status, so the steps keep behaving as the inline shell
they replaced. test_simulation_rollback.py covers how the routing restore
reads the deployment run's kept plans.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
from test_deployment_assets_support import (
    REPOSITORY_ROOT,
    ROLLBACK_WORKFLOW,
    SCRIPTS_DIRECTORY,
    workflow_jobs,
)
from workflow_script_support import FakeCommands, run_script

if TYPE_CHECKING:
    from pathlib import Path

EXECUTOR_DIRECTORY = REPOSITORY_ROOT / "projects" / "policyengine-simulation-executor"
RESTORE_ROUTING = "rollback-restore-routing.sh"
RESOLVE_PENDING = "rollback-resolve-pending-publication.sh"
READ_IDENTITY = "rollback-read-release-identity.sh"
STABLE_SERVICE = "rollback-stable-service-state.sh"
RESTORE_TRAFFIC = "rollback-restore-entrypoint-traffic.sh"
VERIFY_SERVICE = "rollback-verify-stable-service.sh"
TRAFFIC_SCRIPT = "set-cloud-run-simulation-entry-revision.sh"
SMOKE_SCRIPT = "cloud-run-simulation-entry-smoke.sh"
REGISTRY_COMMAND = [
    "uv",
    "run",
    "--frozen",
    "python",
    "-m",
    "src.modal.utils.update_version_registry",
]
SERVICE = "policyengine-simulation-entry"
STABLE_URL = "https://policyengine-simulation-entry-abc-uc.a.run.app"

# Prints FAKE_UV_STDOUT, then fails with status 9 when its arguments include
# FAKE_UV_FAILING_OPTION.
FAKE_UV = """\
printf '%s' "${FAKE_UV_STDOUT:-}"
if [[ " $* " == *" ${FAKE_UV_FAILING_OPTION:-none} "* ]]; then
  exit 9
fi
"""


@pytest.fixture
def fakes(tmp_path: Path) -> FakeCommands:
    commands = FakeCommands.create(tmp_path)
    commands.add("uv", FAKE_UV)
    return commands


def _run(fakes: FakeCommands, tmp_path: Path, script: str, **env: str):
    return run_script(
        script,
        env=fakes.env(
            **{
                "RUNNER_TEMP": str(tmp_path / "runner-temp"),
                "GITHUB_OUTPUT": str(tmp_path / "github-output"),
                "GITHUB_REPOSITORY": "PolicyEngine/policyengine-sim-api",
                "TARGET": "prod",
                "MODAL_ENVIRONMENT": "main",
                "DEPLOY_RUN_ID": "",
                "POLICYENGINE_VERSION": "",
                "EXPECTED_LATEST": "",
                **env,
            }
        ),
        cwd=EXECUTOR_DIRECTORY,
    )


def _github_output(tmp_path: Path) -> str:
    path = tmp_path / "github-output"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --- Restore Modal routing: point latest at a published release -------------


@pytest.mark.parametrize(
    ("expected_latest", "guard"),
    [("", []), ("6.3.0", ["--expected-latest", "6.3.0"])],
    ids=["unguarded", "guarded"],
)
def test_restore_latest_points_latest_at_the_published_release(
    fakes, tmp_path, expected_latest, guard
):
    result = _run(
        fakes,
        tmp_path,
        RESTORE_ROUTING,
        POLICYENGINE_VERSION="6.2.6",
        EXPECTED_LATEST=expected_latest,
    )

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        [
            *REGISTRY_COMMAND,
            "--environment",
            "main",
            "--restore-latest",
            "6.2.6",
            *guard,
        ]
    ]


def test_restore_latest_fails_when_the_publisher_fails(fakes, tmp_path):
    result = _run(
        fakes,
        tmp_path,
        RESTORE_ROUTING,
        POLICYENGINE_VERSION="6.2.6",
        FAKE_UV_FAILING_OPTION="--restore-latest",
    )

    assert result.returncode == 9


# --- Resolve the unfinished routing publication -----------------------------


def test_pending_resolution_prints_the_journal_then_restores_it(fakes, tmp_path):
    result = _run(fakes, tmp_path, RESOLVE_PENDING)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        [*REGISTRY_COMMAND, "--environment", "main", "--print-pending-plan"],
        [
            *REGISTRY_COMMAND,
            "--environment",
            "main",
            "--finalize-publication",
            "--release-environment",
            "prod",
            "--gates-passed",
            "false",
        ],
    ]


def test_pending_resolution_stops_when_the_journal_cannot_be_printed(fakes, tmp_path):
    result = _run(
        fakes, tmp_path, RESOLVE_PENDING, FAKE_UV_FAILING_OPTION="--print-pending-plan"
    )

    assert result.returncode == 9
    assert len(fakes.calls()) == 1


# --- Read the restored release identity -------------------------------------

IDENTITY_STDOUT = (
    "Latest routes to policyengine 6.2.6\n"
    "policyengine_version=6.2.6\n"
    "app_name=policyengine-simulation-py6-2-6\n"
    "uk_model_version=2.126.8\n"
    "uk_dataset_sha256=" + "a" * 64 + "\n"
    "us_model_version=1.690.7\n"
)
IDENTITY_OUTPUTS = (
    "policyengine_version=6.2.6\n"
    "app_name=policyengine-simulation-py6-2-6\n"
    "uk_model_version=2.126.8\n"
    "uk_dataset_sha256=" + "a" * 64 + "\n"
)


def test_identity_records_the_four_identity_outputs(fakes, tmp_path):
    (tmp_path / "github-output").write_text("earlier=kept\n", encoding="utf-8")

    result = _run(fakes, tmp_path, READ_IDENTITY, FAKE_UV_STDOUT=IDENTITY_STDOUT)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        [*REGISTRY_COMMAND, "--environment", "main", "--print-identity"]
    ]
    # tee shows the recorded lines in the step log and appends them.
    assert result.stdout == IDENTITY_OUTPUTS
    assert _github_output(tmp_path) == "earlier=kept\n" + IDENTITY_OUTPUTS


def test_identity_fails_when_the_registry_cannot_be_read(fakes, tmp_path):
    result = _run(
        fakes,
        tmp_path,
        READ_IDENTITY,
        FAKE_UV_STDOUT=IDENTITY_STDOUT,
        FAKE_UV_FAILING_OPTION="--print-identity",
    )

    assert result.returncode == 9


def test_identity_fails_when_the_registry_prints_no_identity(fakes, tmp_path):
    result = _run(fakes, tmp_path, READ_IDENTITY, FAKE_UV_STDOUT="nothing\n")

    # grep exits 1 when no line matches.
    assert result.returncode == 1
    assert _github_output(tmp_path) == ""


# --- Resolve the stable service ---------------------------------------------

FAKE_GCLOUD = """\
printf '%s' "${FAKE_SERVICE_JSON}"
exit "${FAKE_GCLOUD_EXIT:-0}"
"""


def _service_json(traffic: list[dict], url: str | None = STABLE_URL) -> str:
    status: dict = {"traffic": traffic}
    if url is not None:
        status["url"] = url
    return json.dumps({"status": status})


def _stable_service(fakes: FakeCommands, tmp_path: Path, service_json: str, **env):
    fakes.add("gcloud", FAKE_GCLOUD)
    return _run(
        fakes,
        tmp_path,
        STABLE_SERVICE,
        SERVICE=SERVICE,
        PROJECT_ID="policyengine-api",
        REGION="us-central1",
        FAKE_SERVICE_JSON=service_json,
        **env,
    )


def test_stable_service_records_its_url_and_fully_serving_revision(fakes, tmp_path):
    service_json = _service_json(
        [
            {"revisionName": "entry-00001-old", "percent": 100},
            {"revisionName": "entry-00002-new", "tag": "s-41"},
        ]
    )

    result = _stable_service(fakes, tmp_path, service_json)

    assert result.returncode == 0, result.stderr
    assert fakes.calls("gcloud") == [
        [
            "gcloud",
            "run",
            "services",
            "describe",
            SERVICE,
            "--project",
            "policyengine-api",
            "--region",
            "us-central1",
            "--format=json",
        ]
    ]
    assert _github_output(tmp_path) == (f"url={STABLE_URL}\nrevision=entry-00001-old\n")


def test_stable_service_fails_when_the_service_cannot_be_described(fakes, tmp_path):
    result = _stable_service(fakes, tmp_path, "{}", FAKE_GCLOUD_EXIT="1")

    assert result.returncode == 1
    assert _github_output(tmp_path) == ""


@pytest.mark.parametrize(
    ("service_json", "outputs"),
    [
        (
            _service_json(
                [
                    {"revisionName": "entry-00001-one", "percent": 50},
                    {"revisionName": "entry-00002-two", "percent": 50},
                ]
            ),
            f"url={STABLE_URL}\nrevision=\n",
        ),
        (
            _service_json(
                [{"revisionName": "entry-00001-old", "percent": 100}], url=None
            ),
            "url=null\nrevision=entry-00001-old\n",
        ),
    ],
    ids=["split-traffic", "no-url"],
)
def test_stable_service_records_what_jq_prints_when_a_value_is_missing(
    fakes, tmp_path, service_json, outputs
):
    """As in the inline step it replaced, a jq failure inside echo does not stop it.

    The step records an empty revision or a null URL and succeeds; the
    verification step that reads them is what fails.
    """
    result = _stable_service(fakes, tmp_path, service_json)

    assert result.returncode == 0, result.stderr
    assert _github_output(tmp_path) == outputs


# --- Restore entrypoint traffic and verify: scripts that call a sibling -----


def _with_fake_sibling(tmp_path: Path, script: str, sibling: str, body: str) -> Path:
    """Copy ``script`` beside a fake ``sibling`` that records its call and env."""
    directory = tmp_path / "scripts"
    directory.mkdir()
    shutil.copy2(SCRIPTS_DIRECTORY / script, directory / script)
    fake = directory / sibling
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        ': > "${FAKE_SIBLING_ARGUMENTS}"\n'
        'for argument in "$@"; do\n'
        '  printf \'%s\\n\' "${argument}" >> "${FAKE_SIBLING_ARGUMENTS}"\n'
        "done\n"
        'env > "${FAKE_SIBLING_ENVIRONMENT}"\n' + body,
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return directory / script


def _run_copied(fakes: FakeCommands, tmp_path: Path, script: Path, **env: str):
    return subprocess.run(
        [str(script)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPOSITORY_ROOT,
        env=fakes.env(
            FAKE_SIBLING_ARGUMENTS=str(tmp_path / "sibling-arguments"),
            FAKE_SIBLING_ENVIRONMENT=str(tmp_path / "sibling-environment"),
            **env,
        ),
    )


def _sibling_arguments(tmp_path: Path) -> list[str] | None:
    path = tmp_path / "sibling-arguments"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else None


TRAFFIC_ENVIRONMENT = {
    "SERVICE": SERVICE,
    "SIMULATION_ENTRYPOINT_GCP_PROJECT_ID": "policyengine-api",
    "SIMULATION_ENTRYPOINT_GCP_REGION": "us-central1",
    "SIMULATION_ENTRYPOINT_SERVICE": SERVICE,
    "SIMULATION_ENTRYPOINT_EXPECTED_CURRENT_REVISION": "entry-00009-current",
}


def test_traffic_restore_moves_traffic_to_another_revision(fakes, tmp_path):
    script = _with_fake_sibling(tmp_path, RESTORE_TRAFFIC, TRAFFIC_SCRIPT, "")

    result = _run_copied(
        fakes,
        tmp_path,
        script,
        **TRAFFIC_ENVIRONMENT,
        SIMULATION_ENTRYPOINT_TARGET_REVISION="entry-00007-retained",
    )

    assert result.returncode == 0, result.stderr
    assert _sibling_arguments(tmp_path) == []
    environment = (tmp_path / "sibling-environment").read_text()
    for name, value in {
        **TRAFFIC_ENVIRONMENT,
        "SIMULATION_ENTRYPOINT_TARGET_REVISION": "entry-00007-retained",
    }.items():
        assert f"{name}={value}\n" in environment


def test_traffic_restore_leaves_traffic_already_on_the_revision(fakes, tmp_path):
    script = _with_fake_sibling(tmp_path, RESTORE_TRAFFIC, TRAFFIC_SCRIPT, "")

    result = _run_copied(
        fakes,
        tmp_path,
        script,
        **TRAFFIC_ENVIRONMENT,
        SIMULATION_ENTRYPOINT_TARGET_REVISION="entry-00009-current",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{SERVICE} already serves entry-00009-current.\n"
    assert _sibling_arguments(tmp_path) is None


def test_traffic_restore_fails_when_the_traffic_change_fails(fakes, tmp_path):
    script = _with_fake_sibling(tmp_path, RESTORE_TRAFFIC, TRAFFIC_SCRIPT, "exit 3\n")

    result = _run_copied(
        fakes,
        tmp_path,
        script,
        **TRAFFIC_ENVIRONMENT,
        SIMULATION_ENTRYPOINT_TARGET_REVISION="entry-00007-retained",
    )

    assert result.returncode == 3


VERIFY_ENVIRONMENT = {
    "STABLE_URL": STABLE_URL,
    "EXPECTED_REVISION": "entry-00007-retained",
    "PREVIOUS_REVISION": "entry-00009-current",
    "EXPECTED_POLICYENGINE_VERSION": "6.2.6",
}


@pytest.mark.parametrize("requested_version", ["6.2.6", ""], ids=["version", "plans"])
def test_verify_smokes_the_stable_service_for_the_restored_release(
    fakes, tmp_path, requested_version
):
    script = _with_fake_sibling(tmp_path, VERIFY_SERVICE, SMOKE_SCRIPT, "")

    result = _run_copied(
        fakes,
        tmp_path,
        script,
        **VERIFY_ENVIRONMENT,
        POLICYENGINE_VERSION=requested_version,
    )

    assert result.returncode == 0, result.stderr
    assert _sibling_arguments(tmp_path) == [
        STABLE_URL,
        "entry-00007-retained",
        "entry-00009-current",
    ]


def test_verify_refuses_when_latest_routes_to_another_release(fakes, tmp_path):
    script = _with_fake_sibling(tmp_path, VERIFY_SERVICE, SMOKE_SCRIPT, "")

    result = _run_copied(
        fakes, tmp_path, script, **VERIFY_ENVIRONMENT, POLICYENGINE_VERSION="6.3.0"
    )

    assert result.returncode == 1
    assert result.stderr == "latest routes to 6.2.6, not 6.3.0\n"
    assert _sibling_arguments(tmp_path) is None


def test_verify_fails_when_the_smoke_fails(fakes, tmp_path):
    script = _with_fake_sibling(tmp_path, VERIFY_SERVICE, SMOKE_SCRIPT, "exit 4\n")

    result = _run_copied(
        fakes, tmp_path, script, **VERIFY_ENVIRONMENT, POLICYENGINE_VERSION=""
    )

    assert result.returncode == 4


# --- simulation-rollback.yml runs each step from its script -----------------

EXECUTOR_WORKING_DIRECTORY = "projects/policyengine-simulation-executor"
MODAL_CREDENTIALS = {
    "MODAL_TOKEN_ID": "${{ secrets.MODAL_TOKEN_ID }}",
    "MODAL_TOKEN_SECRET": "${{ secrets.MODAL_TOKEN_SECRET }}",
}
NOT_RESOLVING = "${{ !inputs.resolve_pending_publication }}"
# Every key of each step except its name, as before the steps moved into
# scripts, with the run: line that now calls the script.
MOVED_STEPS = {
    "Restore Modal routing": {
        "if": NOT_RESOLVING,
        "working-directory": EXECUTOR_WORKING_DIRECTORY,
        "env": {**MODAL_CREDENTIALS, "GH_TOKEN": "${{ github.token }}"},
        "run": f"../../.github/scripts/{RESTORE_ROUTING}",
    },
    "Resolve the unfinished routing publication": {
        "if": "${{ inputs.resolve_pending_publication }}",
        "working-directory": EXECUTOR_WORKING_DIRECTORY,
        "env": MODAL_CREDENTIALS,
        "run": f"../../.github/scripts/{RESOLVE_PENDING}",
    },
    "Read the restored release identity": {
        "if": NOT_RESOLVING,
        "id": "identity",
        "working-directory": EXECUTOR_WORKING_DIRECTORY,
        "env": MODAL_CREDENTIALS,
        "run": f"../../.github/scripts/{READ_IDENTITY}",
    },
    "Resolve the stable service": {
        "if": NOT_RESOLVING,
        "id": "service",
        "run": f".github/scripts/{STABLE_SERVICE}",
    },
    "Restore entrypoint traffic": {
        "if": "${{ inputs.entrypoint_revision != '' }}",
        "env": {
            "SIMULATION_ENTRYPOINT_GCP_PROJECT_ID": "${{ env.PROJECT_ID }}",
            "SIMULATION_ENTRYPOINT_GCP_REGION": "${{ env.REGION }}",
            "SIMULATION_ENTRYPOINT_SERVICE": "${{ env.SERVICE }}",
            "SIMULATION_ENTRYPOINT_TARGET_REVISION": (
                "${{ inputs.entrypoint_revision }}"
            ),
            "SIMULATION_ENTRYPOINT_EXPECTED_CURRENT_REVISION": (
                "${{ steps.service.outputs.revision }}"
            ),
        },
        "run": f".github/scripts/{RESTORE_TRAFFIC}",
    },
    "Verify the stable service serves the restored release": {
        "if": NOT_RESOLVING,
        "env": {
            "EXPECTED_IDENTITY": "required",
            "EXPECTED_POLICYENGINE_VERSION": (
                "${{ steps.identity.outputs.policyengine_version }}"
            ),
            "EXPECTED_APP_NAME": "${{ steps.identity.outputs.app_name }}",
            "EXPECTED_UK_MODEL_VERSION": (
                "${{ steps.identity.outputs.uk_model_version }}"
            ),
            "EXPECTED_UK_DATASET_SHA256": (
                "${{ steps.identity.outputs.uk_dataset_sha256 || 'unrecorded' }}"
            ),
            "EXPECTED_LATEST": "true",
            "STABLE_URL": "${{ steps.service.outputs.url }}",
            "EXPECTED_REVISION": "${{ inputs.entrypoint_revision }}",
            "PREVIOUS_REVISION": "${{ steps.service.outputs.revision }}",
        },
        "run": f".github/scripts/{VERIFY_SERVICE}",
    },
}


@pytest.mark.parametrize("step_name", list(MOVED_STEPS))
def test_each_moved_rollback_step_runs_its_script_with_unchanged_settings(
    step_name,
):
    steps = workflow_jobs(ROLLBACK_WORKFLOW)["rollback"]["steps"]
    matching = [step for step in steps if step.get("name") == step_name]

    assert len(matching) == 1
    assert {key: value for key, value in matching[0].items() if key != "name"} == (
        MOVED_STEPS[step_name]
    )
