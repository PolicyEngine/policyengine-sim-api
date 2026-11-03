"""The scripts the Public API schema job runs, with fake commands on PATH.

``public-api-schema-commit.sh`` reads the pinned Public API commit,
``public-api-commit-on-default-branch.sh`` requires it to be on the Public
API's default branch, and ``public-api-schema-reference.sh`` builds the
Public API's tables at that commit and grants its declared privileges.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPOSITORY_ROOT / ".github" / "scripts"
COMMIT_SCRIPT = SCRIPTS / "public-api-schema-commit.sh"
DEFAULT_BRANCH_SCRIPT = SCRIPTS / "public-api-commit-on-default-branch.sh"
REFERENCE_SCRIPT = SCRIPTS / "public-api-schema-reference.sh"
PINNED_COMMIT = "0123456789abcdef0123456789abcdef01234567"
DATABASE_URL = (
    "postgresql://postgres:simulation_ci@127.0.0.1:5432/policyengine_v2_alembic_test"
)


def _fake_command(directory: Path, name: str, body: str) -> None:
    """Write an executable that logs its arguments and runs ``body``."""
    command = directory / name
    command.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s|%s\\n' "{name}" "$*" >> "${{FAKE_COMMAND_LOG}}"
            {body}
            """
        ),
        encoding="utf-8",
    )
    command.chmod(0o755)


def _run(script: Path, arguments: list[str], tmp_path: Path, **environment):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / "commands.log"
    log.touch()
    command_environment = {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_COMMAND_LOG": str(log),
        "HOME": str(tmp_path),
    } | environment
    result = subprocess.run(
        ["bash", str(script), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=command_environment,
    )
    return result, log.read_text(encoding="utf-8").splitlines()


# --- The pinned commit ----------------------------------------------------


def test_the_pinned_commit_is_read_and_passed_to_later_steps(tmp_path):
    pin = tmp_path / "public-api-commit"
    pin.write_text(f"{PINNED_COMMIT}\n", encoding="utf-8")
    github_output = tmp_path / "github-output"

    result, _ = _run(
        COMMIT_SCRIPT, [str(pin)], tmp_path, GITHUB_OUTPUT=str(github_output)
    )

    assert result.returncode == 0, result.stderr
    assert f"Pinned Public API commit: {PINNED_COMMIT}" in result.stdout
    assert github_output.read_text(encoding="utf-8") == f"commit={PINNED_COMMIT}\n"


@pytest.mark.parametrize(
    "content",
    ["", "master\n", f"{PINNED_COMMIT[:12]}\n", f"{PINNED_COMMIT.upper()}\n"],
    ids=["empty", "branch", "short", "upper-case"],
)
def test_a_pin_that_is_not_a_full_commit_fails(tmp_path, content):
    pin = tmp_path / "public-api-commit"
    pin.write_text(content, encoding="utf-8")

    result, _ = _run(COMMIT_SCRIPT, [str(pin)], tmp_path)

    assert result.returncode != 0
    assert "40 lowercase hexadecimal characters" in result.stderr


def test_the_repository_pins_a_full_commit(tmp_path):
    result, _ = _run(
        COMMIT_SCRIPT,
        [
            str(
                REPOSITORY_ROOT
                / "libs"
                / "policyengine-simulation-persistence"
                / "public-api-commit"
            )
        ],
        tmp_path,
    )

    assert result.returncode == 0, result.stderr


# --- The default branch ---------------------------------------------------


def _fake_git(tmp_path, *, ancestor: bool) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    _fake_command(
        fake_bin,
        "git",
        textwrap.dedent(
            f"""\
            case "$*" in
              *"rev-parse HEAD"*) printf '%s\\n' "{PINNED_COMMIT}" ;;
              *"merge-base --is-ancestor"*) exit {0 if ancestor else 1} ;;
            esac
            """
        ),
    )


def test_a_commit_on_the_default_branch_passes(tmp_path):
    _fake_git(tmp_path, ancestor=True)

    result, commands = _run(DEFAULT_BRANCH_SCRIPT, ["public-api", "master"], tmp_path)

    assert result.returncode == 0, result.stderr
    assert "git|-C public-api fetch --quiet origin master" in commands
    assert (
        f"git|-C public-api merge-base --is-ancestor {PINNED_COMMIT} FETCH_HEAD"
        in commands
    )


def test_a_commit_not_yet_on_the_default_branch_fails_and_says_so(tmp_path):
    _fake_git(tmp_path, ancestor=False)

    result, _ = _run(DEFAULT_BRANCH_SCRIPT, ["public-api", "master"], tmp_path)

    assert result.returncode == 1
    assert f"{PINNED_COMMIT} is not on the Public API's master branch" in (
        result.stderr
    )


# --- The reference database -----------------------------------------------


def _fake_reference_commands(tmp_path, *, failing_step: str = "") -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    _fake_command(fake_bin, "git", f"printf '%s\\n' '{PINNED_COMMIT}'")
    _fake_command(
        fake_bin,
        "uv",
        textwrap.dedent(
            f"""\
            printf 'uv-env|%s|%s|%s\\n' "${{V2_MIGRATION_DATABASE_URL:-}}" \\
              "${{V2_ALEMBIC_DISPOSABLE_TEST:-}}" "${{DEPLOYMENT_ENVIRONMENT:-}}" \\
              >> "${{FAKE_COMMAND_LOG}}"
            case "$*" in
              *"{failing_step}"*) [ -n "{failing_step}" ] && exit 3 ;;
            esac
            exit 0
            """
        ),
    )


def test_the_reference_migrates_the_public_api_and_grants_its_privileges(tmp_path):
    checkout = tmp_path / "public-api"
    checkout.mkdir()
    github_env = tmp_path / "github-env"
    _fake_reference_commands(tmp_path)

    result, commands = _run(
        REFERENCE_SCRIPT,
        [str(checkout)],
        tmp_path,
        PUBLIC_API_SCHEMA_DATABASE_URL=DATABASE_URL,
        GITHUB_ENV=str(github_env),
    )

    assert result.returncode == 0, result.stderr
    uv_calls = [line for line in commands if line.startswith("uv|")]
    assert uv_calls == [
        "uv|sync --frozen --extra dev",
        "uv|run --frozen --extra dev alembic -c alembic-v2.ini upgrade head",
        "uv|run --frozen --extra dev python scripts/reconcile_v2_privileges.py "
        "apply --create-missing-roles",
    ]
    psycopg_url = "postgresql+psycopg://" + DATABASE_URL.split("://", 1)[1]
    assert f"uv-env|{psycopg_url}|1|ci" in commands
    assert f"Public API commit: {PINNED_COMMIT}" in result.stdout
    assert github_env.read_text(encoding="utf-8") == (
        f"PUBLIC_API_SCHEMA_COMMIT={PINNED_COMMIT}\n"
    )


@pytest.mark.parametrize("failing_step", ["upgrade head", "reconcile_v2_privileges"])
def test_a_failed_reference_step_fails_the_job(tmp_path, failing_step):
    checkout = tmp_path / "public-api"
    checkout.mkdir()
    _fake_reference_commands(tmp_path, failing_step=failing_step)

    result, _ = _run(
        REFERENCE_SCRIPT,
        [str(checkout)],
        tmp_path,
        PUBLIC_API_SCHEMA_DATABASE_URL=DATABASE_URL,
    )

    assert result.returncode == 3


def test_the_reference_needs_a_database_to_migrate(tmp_path):
    checkout = tmp_path / "public-api"
    checkout.mkdir()
    _fake_reference_commands(tmp_path)

    result, commands = _run(REFERENCE_SCRIPT, [str(checkout)], tmp_path)

    assert result.returncode != 0
    assert "PUBLIC_API_SCHEMA_DATABASE_URL" in result.stderr
    assert not [line for line in commands if line.startswith("uv|")]
