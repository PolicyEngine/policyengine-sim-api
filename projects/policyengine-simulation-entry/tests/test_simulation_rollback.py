"""The manual Simulation API rollback workflow and its input validation.

Invariants:
- no rollback input is interpolated into shell source; each reaches the
  scripts through the environment and is validated before anything changes;
- rollbacks share the deployments' concurrency group, so one never races a
  release;
- after restoring, the workflow verifies the stable service serves the
  restored policyengine.py release.
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
        WITHDRAW_VERSION="6.3.0",
        ENTRYPOINT_REVISION="policyengine-simulation-entry-00042-abc",
    )
    assert result.returncode == 0, result.stderr
    assert "Rolling prod back to policyengine 6.2.6, withdrawing 6.3.0" in result.stdout
    assert _validate(TARGET="beta", POLICYENGINE_VERSION="6.2.6rc1").returncode == 0


@pytest.mark.parametrize(
    "env",
    [
        {"TARGET": "staging", "POLICYENGINE_VERSION": "6.2.6"},
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
            "WITHDRAW_VERSION": "6.2.6",
        },
        {
            "TARGET": "prod",
            "POLICYENGINE_VERSION": "6.2.6",
            "ENTRYPOINT_REVISION": "Rev; rm -rf /",
        },
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


def test_rollback_validates_first_and_verifies_last():
    workflow = ROLLBACK_WORKFLOW.read_text(encoding="utf-8")
    validate = workflow.index("validate-simulation-rollback-inputs.sh")
    assert validate < workflow.index("--restore-latest")
    assert validate < workflow.index("set-cloud-run-simulation-entry-revision.sh")
    assert workflow.index("cloud-run-simulation-entry-smoke.sh") > workflow.index(
        "set-cloud-run-simulation-entry-revision.sh"
    )
    assert (
        "EXPECTED_POLICYENGINE_VERSION: ${{ inputs.policyengine_version }}" in workflow
    )


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
