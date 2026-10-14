"""Fixtures and helpers for policyengine package updater script tests."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from fixtures.test_modal_scripts import REPO_ROOT, SCRIPTS_DIR

if TYPE_CHECKING:
    from pathlib import Path

SCRIPT = SCRIPTS_DIR / "update-policyengine-package.sh"


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Return a fake checkout whose ``simulation`` project pins policyengine 4.0.0."""
    project = tmp_path / "simulation"
    project.mkdir(parents=True)

    (project / "pyproject.toml").write_text(
        """[project]
dependencies = ["policyengine[models]==4.0.0"]

[dependency-groups]
modal-simulation-image = ["policyengine[models]==4.0.0", "fastapi>=0.115.0"]
""",
        encoding="utf-8",
    )
    (project / "uv.lock").write_text(
        """[[package]]
name = "policyengine"
version = "4.0.0"

[[package]]
name = "policyengine-core"
version = "0.0.0"

[[package]]
name = "policyengine-us"
version = "1.0.0"

[[package]]
name = "policyengine-uk"
version = "2.0.0"

[[package]]
name = "spm-calculator"
version = "0.1.0"
""",
        encoding="utf-8",
    )

    return tmp_path


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """Return an empty directory for fake executables, to put first on PATH."""
    path = tmp_path / "bin"
    path.mkdir()
    return path


def write_executable(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` and mark it executable."""
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@dataclass(frozen=True)
class FakeGitState:
    """What the fake ``git`` reports about the update branch and working tree.

    ``remote_branch_exists`` and ``diff_has_changes`` set the ``ls-remote``
    and ``diff`` exit codes, and ``show`` prints the update branch's pyproject
    pinning ``branch_pin``.
    """

    remote_branch_exists: bool = False
    diff_has_changes: bool = False
    branch_pin: str = "4.1.0"


def install_fake_git(
    fake_bin: Path,
    *,
    root: Path,
    log: Path,
    state: FakeGitState | None = None,
) -> None:
    """Install a fake ``git`` that appends each call to ``log``.

    It answers ``ls-remote``, ``diff`` and ``show`` as ``state`` says; every
    other command succeeds silently.
    """
    state = state or FakeGitState()
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
  if [[ "{int(state.remote_branch_exists)}" == "1" ]]; then
    exit 0
  fi
  exit 2
fi

if [[ "$1" == "diff" ]]; then
  if [[ "{int(state.diff_has_changes)}" == "1" ]]; then
    exit 1
  fi
  exit 0
fi

if [[ "$1" == "show" ]]; then
  printf '[project]\ndependencies = ["policyengine[models]=={state.branch_pin}"]\n'
  exit 0
fi

exit 0
""",
    )


def install_fake_gh(
    fake_bin: Path,
    *,
    log: Path,
    open_pr: str = "",
    open_issue: str = "679",
) -> None:
    """Install a fake ``gh`` that appends each call to ``log`` and answers canned JSON.

    ``open_issue`` and ``open_pr`` (empty for none) drive the existing issue
    and PR lookups; the first line of a created PR's body is logged too.
    """
    write_executable(
        fake_bin / "gh",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'gh %s\\n' "$*" >> "{log}"

if [[ "$1" == "api" ]]; then
  if [[ -n "{open_issue}" ]]; then
    printf '[[{{"number":%s,"title":"Update policyengine to 4.1.0"}}]]\\n' "{open_issue}"
  else
    printf '[[]]\\n'
  fi
  exit 0
fi

if [[ "$1" == "issue" && "$2" == "view" ]]; then
  printf '{{"number":%s,"state":"OPEN","title":"Update policyengine to 4.1.0"}}\\n' "${{3}}"
  exit 0
fi

if [[ "$1" == "issue" && "$2" == "create" ]]; then
  printf 'https://github.com/PolicyEngine/policyengine-sim-api/issues/680\\n'
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "view" && "$*" == *"number,state"* ]]; then
  printf '%s\\n' "{open_pr}"
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "view" ]]; then
  printf '{{"isDraft":true,"headRepository":{{"nameWithOwner":"PolicyEngine/policyengine-sim-api"}},"headRepositoryOwner":{{"login":"PolicyEngine"}}}}\\n'
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "create" ]]; then
  body_file=""
  previous=""
  for argument in "$@"; do
    if [[ "$previous" == "--body-file" ]]; then
      body_file="$argument"
      break
    fi
    previous="$argument"
  done
  if [[ -n "$body_file" ]]; then
    printf 'pr-body-first-line %s\\n' "$(head -n 1 "$body_file")" >> "{log}"
  fi
  printf 'https://github.com/PolicyEngine/policyengine-sim-api/pull/703\\n'
  exit 0
fi

exit 0
""",  # noqa: E501  # the fake gh prints each JSON response on one line, as gh does
    )


@dataclass(frozen=True)
class BundledVersions:
    """Package versions and UK dataset digest the fake ``uv`` reports for 4.1.0."""

    policyengine_version: str = "4.1.0"
    core_version: str = "999.999.999"
    spm_version: str = "1.0.0"
    us_version: str = "1.1.0"
    uk_version: str = "2.1.0"
    uk_dataset_sha256: str = "a" * 64


def install_fake_uv(
    fake_bin: Path,
    *,
    log: Path,
    bundled: BundledVersions | None = None,
) -> None:
    """Install a fake ``uv`` that appends each call to ``log``.

    Only the updater's isolated ``bundle-versions`` query for policyengine
    4.1.0 prints anything: the ``bundled`` versions as ``key=value`` lines.
    """
    bundled = bundled or BundledVersions()
    write_executable(
        fake_bin / "uv",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'uv %s\\n' "$*" >> "{log}"

if [[ "$1" == "run" \
  && "$*" == *"--isolated --no-project --with policyengine==4.1.0"* \
  && "$*" == *"update_policyengine_package.py bundle-versions"* ]]; then
  echo "policyengine_version={bundled.policyengine_version}"
  echo "policyengine_core_version={bundled.core_version}"
  echo "spm_calculator_version={bundled.spm_version}"
  echo "us_version={bundled.us_version}"
  echo "us_data_version=1.10.0"
  echo "uk_version={bundled.uk_version}"
  echo "uk_data_version=1.20.0"
  echo "uk_data_package_version=1.20.0"
  echo "us_dataset_sha256="
  echo "uk_dataset_sha256={bundled.uk_dataset_sha256}"
  echo "uk_certificate_sha256="
  exit 0
fi

exit 0
""",
    )


def updater_env(fake_bin: Path, **extra: str) -> dict[str, str]:
    """Return this environment with ``fake_bin`` first on PATH, plus ``extra``."""
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
    """Run the updater from the repo root; a non-zero exit is returned, not raised."""
    return subprocess.run(  # noqa: S603  # runs the repository updater script with test-built arguments
        ["bash", str(SCRIPT), *args],  # noqa: S607  # bash from PATH, as the workflow runs the script
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
