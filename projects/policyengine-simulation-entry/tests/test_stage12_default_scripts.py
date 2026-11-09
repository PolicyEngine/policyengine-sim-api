"""Tests for the scripts that move the Stage 12 v2 default after the checks.

Each test runs a script with a fake uv first on PATH and asserts on the
exact publisher calls, $GITHUB_OUTPUT and exit status.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from test_deployment_assets_support import REPOSITORY_ROOT
from workflow_script_support import FakeCommands, run_script

if TYPE_CHECKING:
    from pathlib import Path

EXECUTOR_DIRECTORY = REPOSITORY_ROOT / "projects" / "policyengine-simulation-executor"
MOVE = "stage12-v2-default-move.sh"
VERIFY = "stage12-v2-default-verify.sh"
RESTORE = "stage12-v2-default-restore.sh"
READY = "stage12-v2-default-ready.sh"
PUBLISHER = [
    "uv",
    "run",
    "--frozen",
    "python",
    "-m",
    "src.modal.utils.update_v2_version_manifest",
    "--environment",
    "main",
    "--manifest-name",
    "simulation-api-v2-version-manifest",
]
FAKE_UV = """\
printf '%s' "${FAKE_UV_STDOUT:-}"
if [[ "${FAKE_UV_FAILS:-}" == "true" ]]; then
  exit 9
fi
"""
MOVER_STDOUT = (
    "before_sha256=" + "a" * 64 + "\n"
    "after_sha256=" + "b" * 64 + "\n"
    "release_is_default=true\n"
)


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
                "MODAL_ENVIRONMENT": "main",
                "POLICYENGINE_VERSION": "6.13.0",
                **env,
            }
        ),
        cwd=EXECUTOR_DIRECTORY,
    )


def _plan_file(tmp_path: Path) -> Path:
    return tmp_path / "runner-temp" / "stage12-v2-default" / "plan.json"


def _github_output(tmp_path: Path) -> str:
    path = tmp_path / "github-output"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_move_plans_the_release_and_records_its_outputs(fakes, tmp_path):
    result = _run(fakes, tmp_path, MOVE, FAKE_UV_STDOUT=MOVER_STDOUT)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        [
            *PUBLISHER,
            "--move-default",
            "6.13.0",
            "--plan-out",
            str(_plan_file(tmp_path)),
        ]
    ]
    assert _plan_file(tmp_path).parent.is_dir()
    assert _github_output(tmp_path) == MOVER_STDOUT


@pytest.mark.parametrize(
    "fake_uv",
    [{"FAKE_UV_FAILS": "true", "FAKE_UV_STDOUT": MOVER_STDOUT}, {"FAKE_UV_STDOUT": ""}],
    ids=["publisher-failed", "no-outputs"],
)
def test_move_fails_without_a_completed_move(fakes, tmp_path, fake_uv):
    result = _run(fakes, tmp_path, MOVE, **fake_uv)

    assert result.returncode != 0
    assert _github_output(tmp_path) == ""


@pytest.mark.parametrize(
    ("script", "option"), [(VERIFY, "--verify-plan"), (RESTORE, "--restore-plan")]
)
def test_verify_and_restore_pass_the_kept_plan_to_the_publisher(
    fakes, tmp_path, script, option
):
    _plan_file(tmp_path).parent.mkdir(parents=True)
    _plan_file(tmp_path).write_text("{}")

    result = _run(fakes, tmp_path, script)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [[*PUBLISHER, option, str(_plan_file(tmp_path))]]


@pytest.mark.parametrize("script", [VERIFY, RESTORE])
def test_verify_and_restore_fail_when_the_publisher_fails(fakes, tmp_path, script):
    _plan_file(tmp_path).parent.mkdir(parents=True)
    _plan_file(tmp_path).write_text("{}")

    result = _run(fakes, tmp_path, script, FAKE_UV_FAILS="true")

    assert result.returncode == 9


def test_restore_without_a_plan_has_nothing_to_restore(fakes, tmp_path):
    # This attempt's move failed before it kept a plan, so this attempt wrote
    # nothing. An earlier attempt's move may have, and its plan is that
    # attempt's artifact.
    result = _run(fakes, tmp_path, RESTORE)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == []
    assert "This attempt's move kept no v2 default plan" in result.stdout
    assert "stage12-v2-default-plan" in result.stdout


@pytest.mark.parametrize(
    ("requested", "result", "passes"),
    [
        ("true", "success", True),
        ("true", "failure", False),
        ("true", "skipped", False),
        ("true", "cancelled", False),
        ("false", "skipped", True),
        ("false", "success", False),
    ],
)
def test_ready_requires_the_default_job_exactly_when_stage12_was_requested(
    fakes, requested, result, passes
):
    completed = run_script(
        READY, env=fakes.env(REQUESTED=requested, DEFAULT_RESULT=result)
    )

    assert (completed.returncode == 0) is passes
