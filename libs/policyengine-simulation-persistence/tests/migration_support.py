"""Paths and names shared by the migration and store tests."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_CONFIG_PATH = REPOSITORY_ROOT / "migrations" / "alembic.ini"
MIGRATION_DATABASE_URL_VARIABLE = "SIMULATION_MIGRATION_DATABASE_URL"
MIGRATOR_ROLE = "simulation_migrator"
TEST_ROLE_PASSWORD = "simulation-test-password"


def alembic_config() -> Config:
    """Return the repository's Alembic configuration."""
    return Config(str(ALEMBIC_CONFIG_PATH))
