"""Tests for .github/scripts/check-uv-locks.sh, which checks every uv.lock."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from workflow_script_support import FakeCommands, run_script

if TYPE_CHECKING:
    from pathlib import Path

SCRIPT = "check-uv-locks.sh"


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    for directory in ("libs/one", "projects/two", "scripts/tests/fixtures/three"):
        (repository / directory).mkdir(parents=True)
        (repository / directory / "pyproject.toml").write_text("[project]\n")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    return repository


def test_checks_the_lock_of_every_tracked_project(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    fakes = FakeCommands.create(tmp_path)
    fakes.add("uv")

    result = run_script(SCRIPT, env=fakes.env(), cwd=repository)

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        ["uv", "lock", "--check", "--project", "libs/one"],
        ["uv", "lock", "--check", "--project", "projects/two"],
    ]


def test_fails_after_checking_every_project_when_one_lock_is_stale(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    fakes = FakeCommands.create(tmp_path)
    fakes.add("uv", 'if [[ "$5" == "libs/one" ]]; then exit 1; fi\n')

    result = run_script(SCRIPT, env=fakes.env(), cwd=repository)

    assert result.returncode == 1
    assert "run uv lock --project libs/one" in result.stderr
    assert len(fakes.calls("uv")) == 2
