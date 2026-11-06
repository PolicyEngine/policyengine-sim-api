"""Tests for the routing and release-sentinel scripts the deployment runs.

The routing publication steps (plan, record, publish and finalize) and the
release-sentinel checks in simulation-deploy.reusable.yml each run one of these
scripts. Each test runs a script with a fake uv first on PATH (jq is real) and
asserts on the exact calls, $GITHUB_OUTPUT and exit status, so the steps keep
behaving as the inline shell they replaced.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from test_deployment_assets_support import (
    REPOSITORY_ROOT,
    REUSABLE_DEPLOY_WORKFLOW,
    run_step,
    workflow_jobs,
)
from workflow_script_support import FakeCommands, run_script

if TYPE_CHECKING:
    from pathlib import Path

EXECUTOR_DIRECTORY = REPOSITORY_ROOT / "projects" / "policyengine-simulation-executor"
ROUTING_PLAN = "routing-plan.sh"
ROUTING_PLAN_LATEST = "routing-plan-latest.sh"
ROUTING_RECORD_PLAN = "routing-record-plan.sh"
ROUTING_APPLY_PLAN = "routing-apply-plan.sh"
ROUTING_COMPLETE_PLAN = "routing-complete-plan.sh"
ROUTING_RESTORE_PLAN = "routing-restore-plan.sh"
ROUTING_FINALIZE = "routing-finalize-publication.sh"
SENTINEL_BUNDLE_CHECK = "release-sentinel-check-bundle.sh"
SENTINEL_SERVICE_CHECK = "release-sentinel-check-service.sh"
REGISTRY_COMMAND = [
    "uv",
    "run",
    "--frozen",
    "python",
    "-m",
    "src.modal.utils.update_version_registry",
]
SENTINEL_COMMAND = [
    "uv",
    "run",
    "--frozen",
    "python",
    "-m",
    "src.modal.utils.release_sentinel",
]

# Prints FAKE_UV_STDOUT, then fails with status 9 when its first argument (the
# uv subcommand: run or sync) is FAKE_UV_FAILING_SUBCOMMAND.
FAKE_UV = """\
printf '%s' "${FAKE_UV_STDOUT:-}"
if [[ "$1" == "${FAKE_UV_FAILING_SUBCOMMAND:-none}" ]]; then
  exit 9
fi
"""

PLANNER_STDOUT = (
    "Planned routing for simulation-6.3.0\n"
    "app_name=simulation-6.3.0\n"
    "before_sha256=" + "a" * 64 + "\n"
    "after_sha256=" + "b" * 64 + "\n"
    "advances_latest=true\n"
    "note: before_sha256=not-an-output\n"
)
PLANNER_OUTPUTS = (
    "before_sha256=" + "a" * 64 + "\n"
    "after_sha256=" + "b" * 64 + "\n"
    "advances_latest=true\n"
)


@pytest.fixture
def fakes(tmp_path: Path) -> FakeCommands:
    commands = FakeCommands.create(tmp_path)
    commands.add("uv", FAKE_UV)
    return commands


def _run(fakes: FakeCommands, tmp_path: Path, script: str, *args: str, **env: str):
    return run_script(
        script,
        *args,
        env=fakes.env(
            **{
                "RUNNER_TEMP": str(tmp_path / "runner-temp"),
                "GITHUB_OUTPUT": str(tmp_path / "github-output"),
                "GITHUB_RUN_ID": "744001",
                "GITHUB_RUN_ATTEMPT": "2",
                "MODAL_ENVIRONMENT": "main",
                **env,
            }
        ),
        cwd=EXECUTOR_DIRECTORY,
    )


def _github_output(tmp_path: Path) -> str:
    path = tmp_path / "github-output"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --- update_routing: Plan the routing change --------------------------------


def _plan(fakes: FakeCommands, tmp_path: Path, script: str = ROUTING_PLAN, **env: str):
    return _run(
        fakes,
        tmp_path,
        script,
        **{
            "APP_NAME": "simulation-6.3.0",
            "POLICYENGINE_VERSION": "6.3.0",
            "POLICYENGINE_US_VERSION": "1.690.7",
            "POLICYENGINE_UK_VERSION": "2.126.8",
            "FORCE_LATEST": "false",
            "RELEASE_ENVIRONMENT": "prod",
            "FAKE_UV_STDOUT": PLANNER_STDOUT,
            **env,
        },
    )


def _plan_command(runner_temp: Path, plan_option: str = "--plan-out") -> list[str]:
    return [
        *REGISTRY_COMMAND,
        "--app-name",
        "simulation-6.3.0",
        "--policyengine-version",
        "6.3.0",
        "--us-version",
        "1.690.7",
        "--uk-version",
        "2.126.8",
        "--environment",
        "main",
        plan_option,
        f"{runner_temp}/routing-plan/plan.json",
        "--publication-id",
        "744001-2-prod",
    ]


def test_routing_plan_writes_the_plan_and_records_its_digests(fakes, tmp_path):
    runner_temp = tmp_path / "runner-temp"

    result = _plan(fakes, tmp_path)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [_plan_command(runner_temp)]
    # The planner writes plan.json into this directory, which must exist first.
    assert (runner_temp / "routing-plan").is_dir()
    # The planner's output stays in the step log and in routing-plan.out, and
    # only its three output lines reach $GITHUB_OUTPUT.
    assert result.stdout == PLANNER_STDOUT
    assert (runner_temp / "routing-plan.out").read_text() == PLANNER_STDOUT
    assert _github_output(tmp_path) == PLANNER_OUTPUTS


def test_routing_plan_appends_to_the_step_outputs(fakes, tmp_path):
    (tmp_path / "github-output").write_text("earlier=kept\n", encoding="utf-8")

    result = _plan(fakes, tmp_path)

    assert result.returncode == 0, result.stderr
    assert _github_output(tmp_path) == "earlier=kept\n" + PLANNER_OUTPUTS


def test_routing_plan_never_forces_latest(fakes, tmp_path):
    # The routes plan leaves latest; the latest plan after the checks moves it.
    result = _plan(fakes, tmp_path, FORCE_LATEST="true")

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [_plan_command(tmp_path / "runner-temp")]


def test_latest_plan_writes_the_plan_and_records_its_digests(fakes, tmp_path):
    runner_temp = tmp_path / "runner-temp"

    result = _plan(fakes, tmp_path, ROUTING_PLAN_LATEST)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [_plan_command(runner_temp, "--plan-latest-out")]
    assert (runner_temp / "routing-plan").is_dir()
    assert result.stdout == PLANNER_STDOUT
    assert (runner_temp / "routing-plan.out").read_text() == PLANNER_STDOUT
    assert _github_output(tmp_path) == PLANNER_OUTPUTS


@pytest.mark.parametrize(
    ("force_latest", "forced"),
    [("true", True), ("false", False), ("", False), ("True", False)],
)
def test_latest_plan_forces_latest_only_when_requested(
    fakes, tmp_path, force_latest, forced
):
    result = _plan(fakes, tmp_path, ROUTING_PLAN_LATEST, FORCE_LATEST=force_latest)

    assert result.returncode == 0, result.stderr
    expected = _plan_command(tmp_path / "runner-temp", "--plan-latest-out")
    if forced:
        expected.append("--force-latest")
    assert fakes.calls() == [expected]


@pytest.mark.parametrize(
    ("fake_uv", "status"),
    [
        ({"FAKE_UV_FAILING_SUBCOMMAND": "run"}, 9),
        ({"FAKE_UV_STDOUT": "Planned nothing\n"}, 1),
    ],
    ids=["planner-failed", "no-digests"],
)
def test_latest_plan_fails_without_a_plan(fakes, tmp_path, fake_uv, status):
    result = _plan(fakes, tmp_path, ROUTING_PLAN_LATEST, **fake_uv)

    assert result.returncode == status
    assert _github_output(tmp_path) == ""


def test_routing_plan_fails_when_the_planner_fails(fakes, tmp_path):
    result = _plan(fakes, tmp_path, FAKE_UV_FAILING_SUBCOMMAND="run")

    assert result.returncode == 9
    assert _github_output(tmp_path) == ""


def test_routing_plan_fails_when_the_planner_reports_no_digests(fakes, tmp_path):
    result = _plan(fakes, tmp_path, FAKE_UV_STDOUT="Planned nothing\n")

    assert result.returncode == 1
    assert _github_output(tmp_path) == ""


# --- update_routing: record and publish the kept plan -----------------------


@pytest.mark.parametrize(
    ("script", "option"),
    [
        (ROUTING_RECORD_PLAN, "--record-plan"),
        (ROUTING_APPLY_PLAN, "--apply-plan"),
        (ROUTING_COMPLETE_PLAN, "--complete-plan"),
        (ROUTING_RESTORE_PLAN, "--restore-plan"),
    ],
)
def test_record_and_publish_pass_the_kept_plan_to_the_publisher(
    fakes, tmp_path, script, option
):
    result = _run(fakes, tmp_path, script)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        [
            *REGISTRY_COMMAND,
            "--environment",
            "main",
            option,
            f"{tmp_path / 'runner-temp'}/routing-plan/plan.json",
        ]
    ]


@pytest.mark.parametrize(
    "script",
    [
        ROUTING_RECORD_PLAN,
        ROUTING_APPLY_PLAN,
        ROUTING_COMPLETE_PLAN,
        ROUTING_RESTORE_PLAN,
    ],
)
def test_record_and_publish_fail_when_the_publisher_fails(fakes, tmp_path, script):
    result = _run(fakes, tmp_path, script, FAKE_UV_FAILING_SUBCOMMAND="run")

    assert result.returncode == 9


# --- finalize_routing: Finalize the routing publication ---------------------


def _finalize(fakes: FakeCommands, tmp_path: Path, **env: str):
    return _run(
        fakes,
        tmp_path,
        ROUTING_FINALIZE,
        **{
            "PUBLICATION_RUN": "744001",
            "RELEASE_ENVIRONMENT": "prod",
            "GATES_PASSED": "false",
            **env,
        },
    )


def test_finalize_passes_the_gate_outcome_and_the_step_output_file(fakes, tmp_path):
    result = _finalize(fakes, tmp_path)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        [
            *REGISTRY_COMMAND,
            "--environment",
            "main",
            "--finalize-publication",
            "--publication-run",
            "744001",
            "--release-environment",
            "prod",
            "--gates-passed",
            "false",
            "--github-output",
            str(tmp_path / "github-output"),
        ]
    ]


def test_finalize_fails_when_the_publisher_fails(fakes, tmp_path):
    result = _finalize(fakes, tmp_path, FAKE_UV_FAILING_SUBCOMMAND="run")

    assert result.returncode == 9


# --- sentinel_expected: Check the sentinel names this deployment's bundle ---

BUNDLE_SENTINEL = {
    "policyengine_version": "6.3.0",
    "uk": {"model_version": "2.126.8", "dataset_sha256": "c" * 64},
    "cases": [{"name": "uk-default-policy"}],
}


def _check_bundle(fakes: FakeCommands, tmp_path: Path, sentinel: dict | None):
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    if sentinel is not None:
        (runner_temp / "release-sentinel.json").write_text(json.dumps(sentinel))
    return _run(
        fakes,
        tmp_path,
        SENTINEL_BUNDLE_CHECK,
        EXPECTED_POLICYENGINE_VERSION="6.3.0",
        EXPECTED_UK_MODEL_VERSION="2.126.8",
        EXPECTED_UK_DATASET_SHA256="c" * 64,
    )


def test_bundle_check_accepts_the_sentinel_of_this_deployments_bundle(fakes, tmp_path):
    result = _check_bundle(fakes, tmp_path, BUNDLE_SENTINEL)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "true\n"
    assert fakes.calls() == []


@pytest.mark.parametrize(
    "sentinel",
    [
        {**BUNDLE_SENTINEL, "policyengine_version": "6.2.9"},
        {**BUNDLE_SENTINEL, "uk": {**BUNDLE_SENTINEL["uk"], "model_version": "2.1"}},
        {**BUNDLE_SENTINEL, "uk": {**BUNDLE_SENTINEL["uk"], "dataset_sha256": "d"}},
        {**BUNDLE_SENTINEL, "cases": []},
        {key: value for key, value in BUNDLE_SENTINEL.items() if key != "cases"},
    ],
    ids=["policyengine", "uk-model", "uk-dataset", "no-cases", "cases-missing"],
)
def test_bundle_check_rejects_a_sentinel_of_another_bundle(fakes, tmp_path, sentinel):
    result = _check_bundle(fakes, tmp_path, sentinel)

    # jq -e exits 1 when the last value it prints is false.
    assert result.returncode == 1
    assert result.stdout == "false\n"


def test_bundle_check_fails_without_a_sentinel(fakes, tmp_path):
    result = _check_bundle(fakes, tmp_path, None)

    assert result.returncode == 2


# --- integration and promote_entrypoint: a service reproduces the sentinel --


def test_service_check_installs_then_checks_the_given_service(fakes, tmp_path):
    result = _run(
        fakes, tmp_path, SENTINEL_SERVICE_CHECK, "https://candidate.example.test"
    )

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        ["uv", "sync", "--frozen"],
        [
            *SENTINEL_COMMAND,
            "check",
            "--base-url",
            "https://candidate.example.test",
            "--expected",
            f"{tmp_path / 'runner-temp'}/sentinel/release-sentinel.json",
        ],
    ]


def test_service_check_stops_when_the_install_fails(fakes, tmp_path):
    result = _run(
        fakes,
        tmp_path,
        SENTINEL_SERVICE_CHECK,
        "https://candidate.example.test",
        FAKE_UV_FAILING_SUBCOMMAND="sync",
    )

    assert result.returncode == 9
    assert fakes.calls() == [["uv", "sync", "--frozen"]]


def test_service_check_fails_when_the_service_does_not_reproduce_it(fakes, tmp_path):
    result = _run(
        fakes,
        tmp_path,
        SENTINEL_SERVICE_CHECK,
        "https://candidate.example.test",
        FAKE_UV_FAILING_SUBCOMMAND="run",
    )

    assert result.returncode == 9


def test_service_check_requires_the_service_url(fakes, tmp_path):
    result = _run(fakes, tmp_path, SENTINEL_SERVICE_CHECK)

    assert result.returncode != 0
    assert "usage: release-sentinel-check-service.sh <base-url>" in result.stderr
    assert fakes.calls() == []


# --- simulation-deploy.reusable.yml runs each step from its script ----------

EXECUTOR_WORKING_DIRECTORY = "projects/policyengine-simulation-executor"
MODAL_CREDENTIALS = {
    "MODAL_TOKEN_ID": "${{ secrets.MODAL_TOKEN_ID }}",
    "MODAL_TOKEN_SECRET": "${{ secrets.MODAL_TOKEN_SECRET }}",
    "MODAL_ENVIRONMENT": "${{ inputs.modal_environment }}",
}
SENTINEL_TEST_CREDENTIALS = {
    "SIMULATION_TEST_AUTH_ISSUER": "${{ secrets.SIMULATION_ENTRYPOINT_AUTH_ISSUER }}",
    "SIMULATION_TEST_AUTH_AUDIENCE": (
        "${{ secrets.SIMULATION_ENTRYPOINT_AUTH_AUDIENCE }}"
    ),
    "SIMULATION_TEST_AUTH_CLIENT_ID": (
        "${{ secrets.SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_ID }}"
    ),
    "SIMULATION_TEST_AUTH_CLIENT_SECRET": (
        "${{ secrets.SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_SECRET }}"
    ),
}
# Every key of each step except its name, as before the steps moved into
# scripts, with the run: line that now calls the script.
MOVED_STEPS = {
    ("update_routing", "Plan the routing change"): {
        "id": "plan",
        "working-directory": EXECUTOR_WORKING_DIRECTORY,
        "env": {
            **MODAL_CREDENTIALS,
            "APP_NAME": "${{ needs.deploy_executor.outputs.app_name }}",
            "POLICYENGINE_VERSION": "${{ needs.prepare.outputs.policyengine_version }}",
            "POLICYENGINE_US_VERSION": "${{ needs.prepare.outputs.us_version }}",
            "POLICYENGINE_UK_VERSION": "${{ needs.prepare.outputs.uk_version }}",
            "RELEASE_ENVIRONMENT": "${{ inputs.release_environment }}",
        },
        "run": f"../../.github/scripts/{ROUTING_PLAN}",
    },
    ("update_routing", "Record the unfinished routing publication"): {
        "working-directory": EXECUTOR_WORKING_DIRECTORY,
        "env": MODAL_CREDENTIALS,
        "run": f"../../.github/scripts/{ROUTING_RECORD_PLAN}",
    },
    ("update_routing", "Publish the planned routing"): {
        "working-directory": EXECUTOR_WORKING_DIRECTORY,
        "env": MODAL_CREDENTIALS,
        "run": f"../../.github/scripts/{ROUTING_APPLY_PLAN}",
    },
    ("sentinel_expected", "Check the sentinel names this deployment's bundle"): {
        "env": {
            "EXPECTED_POLICYENGINE_VERSION": (
                "${{ needs.prepare.outputs.policyengine_version }}"
            ),
            "EXPECTED_UK_MODEL_VERSION": "${{ needs.prepare.outputs.uk_version }}",
            "EXPECTED_UK_DATASET_SHA256": (
                "${{ needs.prepare.outputs.uk_dataset_sha256 }}"
            ),
        },
        "run": f".github/scripts/{SENTINEL_BUNDLE_CHECK}",
    },
    ("integration", "Check the deployed stack reproduces the sentinel numbers"): {
        "if": "${{ inputs.deploy_existing_stack }}",
        "working-directory": EXECUTOR_WORKING_DIRECTORY,
        "env": SENTINEL_TEST_CREDENTIALS,
        "run": (
            f"../../.github/scripts/{SENTINEL_SERVICE_CHECK} "
            '"${{ needs.deploy_entrypoint.outputs.candidate_url }}"'
        ),
    },
    (
        "promote_entrypoint",
        "Verify the stable service reproduces the sentinel numbers",
    ): {
        "if": "${{ inputs.deploy_existing_stack }}",
        "working-directory": EXECUTOR_WORKING_DIRECTORY,
        "env": SENTINEL_TEST_CREDENTIALS,
        "run": f'../../.github/scripts/{SENTINEL_SERVICE_CHECK} "${{STABLE_URL}}"',
    },
    ("finalize_routing", "Finalize the routing publication"): {
        "id": "finalize",
        "working-directory": EXECUTOR_WORKING_DIRECTORY,
        "env": {
            **MODAL_CREDENTIALS,
            "PUBLICATION_RUN": "${{ github.run_id }}",
            "RELEASE_ENVIRONMENT": "${{ inputs.release_environment }}",
            "GATES_PASSED": (
                "${{\n"
                "  needs.update_routing.result == 'success' &&\n"
                "  needs.integration.result == 'success' &&\n"
                "  needs.authenticated_test.result == 'success' &&\n"
                "  (!inputs.promote_entrypoint || "
                "needs.promote_entrypoint.result == 'success') &&\n"
                "  needs.publish_latest.result == 'success'\n"
                "}}"
            ),
        },
        "run": f"../../.github/scripts/{ROUTING_FINALIZE}",
    },
}


def _named_step(job_name: str, step_name: str) -> dict:
    steps = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)[job_name]["steps"]
    matching = [step for step in steps if step.get("name") == step_name]
    assert len(matching) == 1, (job_name, step_name)
    return matching[0]


@pytest.mark.parametrize(
    ("job_name", "step_name"),
    list(MOVED_STEPS),
    ids=[f"{job}-{step}" for job, step in MOVED_STEPS],
)
def test_each_moved_step_runs_its_script_with_unchanged_settings(job_name, step_name):
    step = _named_step(job_name, step_name)

    assert {key: value for key, value in step.items() if key != "name"} == (
        MOVED_STEPS[job_name, step_name]
    )


def test_the_routing_jobs_keep_reading_the_scripts_outputs():
    jobs = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)

    assert jobs["update_routing"]["outputs"] == {
        "before_sha256": "${{ steps.plan.outputs.before_sha256 }}",
        "after_sha256": "${{ steps.plan.outputs.after_sha256 }}",
    }
    assert jobs["publish_latest"]["outputs"] == {
        "advances_latest": "${{ steps.plan.outputs.advances_latest }}",
    }
    assert jobs["finalize_routing"]["outputs"] == {
        "outcome": "${{ steps.finalize.outputs.outcome }}",
    }


@pytest.mark.parametrize(
    ("job_name", "step_name", "base_url"),
    [
        (
            "integration",
            "Check the deployed stack reproduces the sentinel numbers",
            "https://candidate.example.test",
        ),
        (
            "promote_entrypoint",
            "Verify the stable service reproduces the sentinel numbers",
            "https://stable.example.test",
        ),
    ],
    ids=["candidate", "stable"],
)
def test_each_sentinel_check_step_checks_its_own_service(
    fakes, tmp_path, job_name, step_name, base_url
):
    step = _named_step(job_name, step_name)

    result = run_step(
        step,
        replacements={
            "${{ needs.deploy_entrypoint.outputs.candidate_url }}": (
                "https://candidate.example.test"
            )
        },
        env=fakes.env(
            RUNNER_TEMP=str(tmp_path / "runner-temp"),
            STABLE_URL="https://stable.example.test",
        ),
    )

    assert result.returncode == 0, result.stderr
    sentinel_check = fakes.calls("uv")[-1]
    assert sentinel_check[sentinel_check.index("--base-url") + 1] == base_url
