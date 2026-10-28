"""The CI job that builds the Public API's tables and compares sim's copies.

Rule sim.persistence.public-api-copies-match. The ``public-api-schema`` job
in ``pr.yml`` checks out the Public API's default branch, builds its tables
with its own v2 migration chain
(``.github/scripts/public-api-schema-reference.sh``), and runs the
comparison in ``test_public_api_schema``. It reads no secret.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PR_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "pr.yml"
REFERENCE_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "public-api-schema-reference.sh"
)
JOB = "public-api-schema"
REFERENCE_URL = (
    "postgresql://postgres:simulation_ci@127.0.0.1:5432/policyengine_v2_alembic_test"
)


def _job() -> dict:
    workflow = yaml.safe_load(PR_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"][JOB]


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_the_job_compares_with_the_public_apis_default_branch() -> None:
    job = _job()
    checkout = _step(job, "Check out the Public API's default branch")

    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {
        "repository": "PolicyEngine/policyengine-api",
        "path": "public-api",
        "persist-credentials": False,
    }
    assert _step(job, "Build the Public API's tables from its v2 migration chain")[
        "run"
    ] == (".github/scripts/public-api-schema-reference.sh public-api")
    compare = _step(job, "Compare the test copies with the Public API's tables")
    assert compare["working-directory"] == "libs/policyengine-simulation-persistence"
    assert compare["run"] == "uv run --frozen pytest tests/ -v -m public_api_schema"


def test_the_job_needs_no_secret_and_fails_instead_of_skipping() -> None:
    job = _job()

    assert "secrets" not in yaml.safe_dump(job)
    assert job["permissions"] == {"contents": "read"}
    assert job["env"]["PUBLIC_API_SCHEMA_DATABASE_URL"] == REFERENCE_URL
    assert job["env"]["SIMULATION_REQUIRE_PUBLIC_API_SCHEMA"] == "1"
    assert job["env"]["SIMULATION_REQUIRE_DATABASE_TESTS"] == "1"
    # The Public API's disposable migration mode accepts only this database.
    assert (
        job["services"]["postgres"]["env"]["POSTGRES_DB"]
        == "policyengine_v2_alembic_test"
    )


def _fake_commands(
    tmp_path: Path, *, failing_sync: bool, failing_git: bool = False
) -> tuple[dict, Path]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    calls = tmp_path / "calls.txt"
    sync_exit = "1" if failing_sync else "0"
    (bin_directory / "uv").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "uv $* | UV_PYTHON=${{UV_PYTHON:-}} '
        "V2_MIGRATION_DATABASE_URL=${V2_MIGRATION_DATABASE_URL:-} "
        f'V2_ALEMBIC_DISPOSABLE_TEST=${{V2_ALEMBIC_DISPOSABLE_TEST:-}}" >> {calls}\n'
        f'if [ "$1" = sync ]; then exit {sync_exit}; fi\n',
        encoding="utf-8",
    )
    (bin_directory / "git").write_text(
        "#!/usr/bin/env bash\n"
        + (
            "echo 'fatal: not a git repository' >&2\nexit 128\n"
            if failing_git
            else "echo 0123456789abcdef\n"
        ),
        encoding="utf-8",
    )
    for command in bin_directory.iterdir():
        command.chmod(0o755)
    environment = {
        "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
        "PUBLIC_API_SCHEMA_DATABASE_URL": REFERENCE_URL,
    }
    return environment, calls


def _run_script(environment: dict, checkout: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(REFERENCE_SCRIPT), str(checkout)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_script_migrates_the_database_with_the_public_apis_chain(
    tmp_path: Path,
) -> None:
    environment, calls = _fake_commands(tmp_path, failing_sync=False)
    checkout = tmp_path / "public-api"
    checkout.mkdir()

    github_environment = tmp_path / "github_env"
    environment["GITHUB_ENV"] = str(github_environment)

    result = _run_script(environment, checkout)

    assert result.returncode == 0, result.stderr
    assert "Public API commit: 0123456789abcdef" in result.stdout
    assert github_environment.read_text(encoding="utf-8") == (
        "PUBLIC_API_SCHEMA_COMMIT=0123456789abcdef\n"
    )
    migration_url = REFERENCE_URL.replace("postgresql://", "postgresql+psycopg://")
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "uv sync --frozen --extra dev | UV_PYTHON=3.12 "
        "V2_MIGRATION_DATABASE_URL= V2_ALEMBIC_DISPOSABLE_TEST=",
        "uv run --frozen --extra dev alembic -c alembic-v2.ini upgrade head | "
        f"UV_PYTHON=3.12 V2_MIGRATION_DATABASE_URL={migration_url} "
        "V2_ALEMBIC_DISPOSABLE_TEST=1",
    ]


def test_the_script_stops_when_the_public_api_cannot_install(tmp_path: Path) -> None:
    environment, calls = _fake_commands(tmp_path, failing_sync=True)
    checkout = tmp_path / "public-api"
    checkout.mkdir()

    result = _run_script(environment, checkout)

    assert result.returncode != 0
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1


def test_the_script_needs_the_database_to_migrate(tmp_path: Path) -> None:
    environment, calls = _fake_commands(tmp_path, failing_sync=False)
    del environment["PUBLIC_API_SCHEMA_DATABASE_URL"]
    checkout = tmp_path / "public-api"
    checkout.mkdir()

    result = _run_script(environment, checkout)

    assert result.returncode != 0
    assert "PUBLIC_API_SCHEMA_DATABASE_URL" in result.stderr
    assert not calls.exists()


def test_the_script_stops_when_it_cannot_name_the_commit_it_checks(
    tmp_path: Path,
) -> None:
    """A failed comparison must say which Public API commit it compared with."""
    environment, calls = _fake_commands(tmp_path, failing_sync=False, failing_git=True)
    checkout = tmp_path / "public-api"
    checkout.mkdir()

    result = _run_script(environment, checkout)

    assert result.returncode != 0
    assert not calls.exists()
