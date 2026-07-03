"""Fixtures and helpers for policyengine package updater script tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from fixtures.test_modal_scripts import REPO_ROOT, SCRIPTS_DIR

SCRIPT = SCRIPTS_DIR / "update-policyengine-package.sh"


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    project = tmp_path / "simulation"
    project.mkdir(parents=True)

    (project / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'dependencies = ["policyengine==4.0.0", "policyengine-core==0.0.0", "policyengine-us==1.0.0", "policyengine-uk==2.0.0"]',
            ]
        ),
        encoding="utf-8",
    )
    (project / "uv.lock").write_text(
        "\n".join(
            [
                "[[package]]",
                'name = "policyengine"',
                'version = "4.0.0"',
                "",
                "[[package]]",
                'name = "policyengine-core"',
                'version = "0.0.0"',
                "",
                "[[package]]",
                'name = "policyengine-us"',
                'version = "1.0.0"',
                "",
                "[[package]]",
                'name = "policyengine-uk"',
                'version = "2.0.0"',
            ]
        ),
        encoding="utf-8",
    )

    return tmp_path


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    path = tmp_path / "bin"
    path.mkdir()
    return path


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def install_fake_git(
    fake_bin: Path,
    *,
    root: Path,
    log: Path,
    remote_branch_exists: bool = False,
    diff_has_changes: bool = False,
) -> None:
    write_executable(
        fake_bin / "git",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'git %s\\n' "$*" >> "{log}"

if [[ "$1" == "rev-parse" && "$2" == "--show-toplevel" ]]; then
  echo "{root}"
  exit 0
fi

if [[ "$1" == "ls-remote" ]]; then
  if [[ "{int(remote_branch_exists)}" == "1" ]]; then
    exit 0
  fi
  exit 2
fi

if [[ "$1" == "diff" ]]; then
  if [[ "{int(diff_has_changes)}" == "1" ]]; then
    exit 1
  fi
  exit 0
fi

exit 0
""",
    )


def install_fake_gh(fake_bin: Path, *, log: Path, open_pr: str = "") -> None:
    write_executable(
        fake_bin / "gh",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'gh %s\\n' "$*" >> "{log}"

if [[ "$1" == "pr" && "$2" == "list" ]]; then
  printf '%s\\n' "{open_pr}"
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "create" ]]; then
  exit 0
fi

exit 0
""",
    )


def install_fake_uv(
    fake_bin: Path,
    *,
    log: Path,
    bundled_core_version: str = "999.999.999",
    bundled_us_version: str = "1.1.0",
    bundled_uk_version: str = "2.1.0",
) -> None:
    write_executable(
        fake_bin / "uv",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'uv %s\\n' "$*" >> "{log}"

if [[ "$1" == "run" && "$2" == "python" && "$3" == "-m" && "$4" == "src.modal.utils.extract_bundle_versions" ]]; then
  echo "policyengine_version=4.1.0"
  echo "policyengine_core_version={bundled_core_version}"
  echo "us_version={bundled_us_version}"
  echo "us_data_version=1.10.0"
  echo "uk_version={bundled_uk_version}"
  echo "uk_data_version=1.20.0"
  exit 0
fi

exit 0
""",
    )


def updater_env(fake_bin: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "PROJECT_DIR": "simulation",
        }
    )
    env.update(extra)
    return env


def run_updater(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
