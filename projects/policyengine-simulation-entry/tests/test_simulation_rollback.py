"""The manual Simulation API rollback workflow and its input validation.

Invariants:
- no rollback input is interpolated into shell source; each reaches the
  scripts through the environment and is validated before anything changes;
- rollbacks share the deployments' concurrency group, so one never races a
  release;
- exactly one restore mode is given: a deployment run whose kept routing plan
  is restored, or a published version to point latest at;
- after restoring, the workflow verifies the stable service serves the whole
  identity latest now routes to (version, app, UK model and dataset digest).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ROLLBACK_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "simulation-rollback.yml"
)
DEPLOY_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "simulation-deploy.yml"
VALIDATOR = (
    REPOSITORY_ROOT / ".github" / "scripts" / "validate-simulation-rollback-inputs.sh"
)


def _validate(**env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", VALIDATOR],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **env},
    )


def test_valid_rollbacks_pass_validation():
    result = _validate(
        TARGET="prod",
        POLICYENGINE_VERSION="6.2.6",
        EXPECTED_LATEST="6.3.0",
        ENTRYPOINT_REVISION="policyengine-simulation-entry-00042-abc",
    )
    assert result.returncode == 0, result.stderr
    assert "Rolling prod back to policyengine 6.2.6, entrypoint" in result.stdout
    assert _validate(TARGET="beta", POLICYENGINE_VERSION="6.2.6rc1").returncode == 0
    plan = _validate(TARGET="beta", DEPLOY_RUN_ID="18273645")
    assert plan.returncode == 0, plan.stderr
    assert "Undoing the routing change of beta deployment run 18273645" in plan.stdout


@pytest.mark.parametrize(
    "env",
    [
        {"TARGET": "staging", "POLICYENGINE_VERSION": "6.2.6"},
        {"TARGET": "prod"},
        {"TARGET": "prod", "POLICYENGINE_VERSION": ""},
        {"TARGET": "prod", "POLICYENGINE_VERSION": "6.2.6; curl evil"},
        {"TARGET": "prod", "POLICYENGINE_VERSION": "$(id)"},
        {
            "TARGET": "prod",
            "POLICYENGINE_VERSION": "6.2.6",
            "EXPECTED_LATEST": "latest",
        },
        {
            "TARGET": "prod",
            "POLICYENGINE_VERSION": "6.2.6",
            "ENTRYPOINT_REVISION": "Rev; rm -rf /",
        },
        {"TARGET": "prod", "DEPLOY_RUN_ID": "123", "POLICYENGINE_VERSION": "6.2.6"},
        {"TARGET": "prod", "DEPLOY_RUN_ID": "12x"},
        {"TARGET": "prod", "DEPLOY_RUN_ID": "0"},
        {"TARGET": "prod", "DEPLOY_RUN_ID": "123", "EXPECTED_LATEST": "6.2.6"},
    ],
)
def test_invalid_rollbacks_are_refused_before_any_change(env):
    result = _validate(**env)
    assert result.returncode == 2
    assert "Rollback input error" in result.stderr


def test_rollback_inputs_never_reach_shell_source():
    workflow = ROLLBACK_WORKFLOW.read_text(encoding="utf-8")
    run_blocks = re.findall(r"run: \|\n((?:          .*\n|\n)+)", workflow)
    run_blocks += re.findall(r"run: ([^|\n].*)\n", workflow)
    assert run_blocks
    for block in run_blocks:
        assert "${{ inputs." not in block, block


def _rollback_steps() -> list[dict]:
    import yaml

    workflow = yaml.safe_load(ROLLBACK_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["rollback"]["steps"]


def _step_index(steps, fragment):
    return next(
        index
        for index, step in enumerate(steps)
        if fragment in step.get("run", "") or fragment in step.get("uses", "")
    )


def test_rollback_validates_first_and_verifies_last():
    steps = _rollback_steps()
    validate = _step_index(steps, "validate-simulation-rollback-inputs.sh")
    restore = _step_index(steps, "--restore-plan")
    assert restore == _step_index(steps, "--restore-latest")  # one step, two modes
    identity = _step_index(steps, "--print-identity")
    traffic = _step_index(steps, "set-cloud-run-simulation-entry-revision.sh")
    verify = _step_index(steps, "cloud-run-simulation-entry-smoke.sh")
    assert validate < restore < identity < verify
    assert validate < traffic < verify
    download = steps[_step_index(steps, "actions/download-artifact@")]
    assert download["with"]["run-id"] == "${{ inputs.deploy_run_id }}"
    assert download["with"]["pattern"] == "routing-plan-${{ inputs.target }}-*"
    assert validate < steps.index(download) < restore


def test_rollback_verifies_the_whole_restored_identity():
    """#743/#744: the smoke's identity is all or nothing, so pass all of it."""

    verify = _rollback_steps()[
        _step_index(_rollback_steps(), "cloud-run-simulation-entry-smoke.sh")
    ]
    assert verify["env"]["EXPECTED_IDENTITY"] == "required"
    assert verify["env"]["EXPECTED_LATEST"] == "true"
    for name, output in (
        ("EXPECTED_POLICYENGINE_VERSION", "policyengine_version"),
        ("EXPECTED_APP_NAME", "app_name"),
        ("EXPECTED_UK_MODEL_VERSION", "uk_model_version"),
    ):
        assert verify["env"][name] == f"${{{{ steps.identity.outputs.{output} }}}}"
    assert verify["env"]["EXPECTED_UK_DATASET_SHA256"] == (
        "${{ steps.identity.outputs.uk_dataset_sha256 || 'unrecorded' }}"
    )
    # A version-mode rollback must have made that version latest.
    assert (
        '"${EXPECTED_POLICYENGINE_VERSION}" != "${POLICYENGINE_VERSION}"'
        in verify["run"]
    )


def test_rollback_picks_the_latest_attempts_plan(tmp_path):
    """The plan-selection shell picks the highest run attempt, whatever the path."""

    restore = _rollback_steps()[_step_index(_rollback_steps(), "--restore-plan")]
    selection = restore["run"].split('if [[ ! -f "${plan}" ]]')[0]
    selection = selection.split("then\n", 1)[1]
    runner_temp = tmp_path / "runner-temp-with-dashes"
    for attempt in (1, 3, 2):
        directory = runner_temp / "routing-plans" / f"routing-plan-prod-{attempt}"
        directory.mkdir(parents=True)
        (directory / "plan.json").write_text("{}")
    result = subprocess.run(
        ["bash", "-c", selection + 'printf "%s" "${plan}"'],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "RUNNER_TEMP": str(runner_temp),
            "TARGET": "prod",
        },
    )
    assert result.stdout.endswith("routing-plan-prod-3/plan.json")


def test_rollbacks_share_the_deployment_concurrency_group():
    group = re.compile(r"^concurrency:\n(?:  #.*\n)*  group: (?P<group>\S+)\n", re.M)
    rollback = group.search(ROLLBACK_WORKFLOW.read_text(encoding="utf-8"))
    deploy = group.search(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
    assert rollback and deploy
    assert rollback.group("group") == deploy.group("group") == "simulation-deploy-main"
    assert "cancel-in-progress: false" in ROLLBACK_WORKFLOW.read_text(encoding="utf-8")


def test_validator_has_valid_shell_syntax_and_is_executable():
    subprocess.run(["bash", "-n", VALIDATOR], check=True)
    assert os.access(VALIDATOR, os.X_OK)
