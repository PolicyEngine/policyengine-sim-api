"""The deploy job's migration script, run for real against PostgreSQL."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from migration_support import MIGRATION_DATABASE_URL_VARIABLE, REPOSITORY_ROOT
from policyengine_simulation_persistence import (
    HEAD_REVISION,
    create_simulation_engine,
    read_schema_revision,
)

pytestmark = pytest.mark.postgres

SCRIPT = REPOSITORY_ROOT / ".github" / "scripts" / "migrate-simulation-database.sh"
LIBRARY_DIRECTORY = Path(__file__).resolve().parents[1]


def _run_script(database, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=LIBRARY_DIRECTORY,
        env={
            **os.environ,
            MIGRATION_DATABASE_URL_VARIABLE: database.migrator_url,
            "SIMULATION_MIGRATION_MODE": mode,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _revision(database) -> str | None:
    engine = create_simulation_engine(database.migrator_url)
    try:
        with engine.connect() as connection:
            return read_schema_revision(connection)
    finally:
        engine.dispose()


def test_verify_mode_refuses_a_database_behind_the_chain(disposable_database) -> None:
    result = _run_script(disposable_database, "verify")

    assert result.returncode != 0
    assert _revision(disposable_database) is None


def test_upgrade_mode_migrates_and_then_verify_mode_passes(disposable_database) -> None:
    upgraded = _run_script(disposable_database, "upgrade")
    assert upgraded.returncode == 0, upgraded.stderr
    assert _revision(disposable_database) == HEAD_REVISION

    verified = _run_script(disposable_database, "verify")
    assert verified.returncode == 0, verified.stderr
    assert "simulation-test-password" not in upgraded.stdout + upgraded.stderr
