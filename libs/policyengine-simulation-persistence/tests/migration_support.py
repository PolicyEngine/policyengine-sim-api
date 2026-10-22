"""Paths and names shared by the migration and store tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config
from policyengine_simulation_persistence import (
    create_simulation_engine,
    read_schema_revision,
)

if TYPE_CHECKING:
    from collections.abc import Callable

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_CONFIG_PATH = REPOSITORY_ROOT / "migrations" / "alembic.ini"
MIGRATION_DATABASE_URL_VARIABLE = "SIMULATION_MIGRATION_DATABASE_URL"
MIGRATOR_ROLE = "simulation_migrator"
TEST_ROLE_PASSWORD = "simulation-test-password"


def alembic_config() -> Config:
    """Return the repository's Alembic configuration."""
    return Config(str(ALEMBIC_CONFIG_PATH))


def chain_config(script_location: Path) -> Config:
    """Return a configuration for the chain at ``script_location``."""
    config = Config()
    config.set_main_option("script_location", str(script_location))
    return config


def _step_failure(name: str, step: Callable[[], object]) -> str | None:
    try:
        step()
    except Exception as error:  # noqa: BLE001  # reported as the cycle's failure
        detail = str(error).strip().splitlines()
        return f"{name} failed: {type(error).__name__}" + (
            f": {detail[0]}" if detail else ""
        )
    return None


def migration_cycle_failures(config: Config, *, check: bool) -> list[str]:
    """Upgrade, downgrade and upgrade the chain again; return what failed.

    With ``check``, ``alembic check`` must also find nothing to change after
    each upgrade. The cycle stops at the first failure.
    """
    steps: list[tuple[str, Callable[[], object]]] = [
        ("upgrade", lambda: command.upgrade(config, "head"))
    ]
    if check:
        steps.append(("check", lambda: command.check(config)))
    steps.append(("downgrade", lambda: command.downgrade(config, "base")))
    steps.extend(steps[: 2 if check else 1])
    for name, step in steps:
        failure = _step_failure(name, step)
        if failure is not None:
            return [failure]
    return []


def applied_revision(database_url: str) -> str | None:
    """Return the revision the database at ``database_url`` has applied."""
    engine = create_simulation_engine(database_url)
    try:
        with engine.connect() as connection:
            return read_schema_revision(connection)
    finally:
        engine.dispose()
