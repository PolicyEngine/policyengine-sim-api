"""Alembic environment for the Simulation Entrypoint's ``simulation`` schema."""

from __future__ import annotations

import os
from logging.config import fileConfig
from typing import TYPE_CHECKING

from alembic import context
from policyengine_simulation_persistence import (
    ALEMBIC_VERSION_TABLE,
    SIMULATION_SCHEMA,
    normalized_database_url,
    simulation_metadata,
)
from sqlalchemy import create_engine, inspect, pool
from sqlalchemy.schema import CreateSchema

if TYPE_CHECKING:
    from sqlalchemy import Connection

DATABASE_URL_VARIABLE = "SIMULATION_MIGRATION_DATABASE_URL"

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _include_name(name: str | None, type_: str, _parent_names: object) -> bool:
    """Compare only the simulation schema, never other services' schemas."""
    if type_ == "schema":
        return name == SIMULATION_SCHEMA
    return True


def _include_object(
    _object: object,
    name: str | None,
    type_: str,
    _reflected: bool,  # noqa: FBT001  # Alembic passes these positionally
    _compare_to: object,
) -> bool:
    """Leave Alembic's own version table out of the schema comparison."""
    return not (type_ == "table" and name == ALEMBIC_VERSION_TABLE)


def _ensure_simulation_schema(connection: Connection) -> None:
    """Create the simulation schema, owned by the migrator, if it is missing.

    PostgreSQL checks CREATE on the database before it checks whether a schema
    exists, even for CREATE SCHEMA IF NOT EXISTS. Creating the schema only when
    it is missing lets an operator create it in advance, owned by the migrator,
    instead of granting the migrator CREATE on the database.
    """
    if not inspect(connection).has_schema(SIMULATION_SCHEMA):
        connection.execute(CreateSchema(SIMULATION_SCHEMA))


def _run(connection: Connection) -> None:
    # The version table lives in the simulation schema, so the schema must
    # exist before Alembic looks for it.
    _ensure_simulation_schema(connection)
    context.configure(
        connection=connection,
        target_metadata=simulation_metadata,
        version_table=ALEMBIC_VERSION_TABLE,
        version_table_schema=SIMULATION_SCHEMA,
        include_schemas=True,
        include_name=_include_name,
        include_object=_include_object,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations to the database named by the migration URL."""
    database_url = os.environ.get(DATABASE_URL_VARIABLE, "")
    if not database_url:
        msg = f"{DATABASE_URL_VARIABLE} must name the simulation database"
        raise RuntimeError(msg)
    engine = create_engine(
        normalized_database_url(database_url),
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": 10, "prepare_threshold": None},
    )
    try:
        # One transaction covers the schema creation and every revision, so a
        # failed migration leaves nothing half-applied.
        with engine.begin() as connection:
            _run(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    msg = (
        "Simulation migrations run only online, against the database they "
        "change; offline SQL generation is not supported"
    )
    raise RuntimeError(msg)
run_migrations_online()
