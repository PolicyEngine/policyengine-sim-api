"""Run the simulation migration chain against a fresh PostgreSQL database."""

from __future__ import annotations

import pytest
from alembic import command
from migration_support import MIGRATOR_ROLE, alembic_config
from policyengine_simulation_persistence import (
    HEAD_REVISION,
    SIMULATION_SCHEMA,
    create_simulation_engine,
    read_schema_revision,
)
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.postgres


def _revision(database_url: str) -> str | None:
    engine = create_simulation_engine(database_url)
    try:
        with engine.connect() as connection:
            return read_schema_revision(connection)
    finally:
        engine.dispose()


def _superuser_engine(database, **options):
    return create_engine(
        database.superuser_url.replace("postgresql://", "postgresql+psycopg://", 1),
        **options,
    )


def test_upgrade_check_downgrade_upgrade_cycle(disposable_database) -> None:
    config = alembic_config()

    command.upgrade(config, "head")
    assert _revision(disposable_database.migrator_url) == HEAD_REVISION
    command.check(config)

    command.downgrade(config, "base")
    assert _revision(disposable_database.migrator_url) is None

    command.upgrade(config, "head")
    assert _revision(disposable_database.migrator_url) == HEAD_REVISION
    command.check(config)


def test_the_version_table_lives_only_in_the_simulation_schema(
    disposable_database,
) -> None:
    command.upgrade(alembic_config(), "head")

    engine = _superuser_engine(disposable_database)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert inspector.has_table("alembic_version", schema=SIMULATION_SCHEMA)
            assert not inspector.has_table("alembic_version", schema="public")
            schema_owner = connection.execute(
                text(
                    "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
                    "WHERE nspname = :schema"
                ),
                {"schema": SIMULATION_SCHEMA},
            ).scalar()
    finally:
        engine.dispose()
    assert schema_owner == MIGRATOR_ROLE


def test_an_unmigrated_database_reports_no_revision(disposable_database) -> None:
    assert _revision(disposable_database.migrator_url) is None


def _precreate_schema_without_database_create(database) -> None:
    """Set up the operator alternative to granting the migrator CREATE.

    The superuser takes over the database, so the migrator keeps only the
    default CONNECT and TEMPORARY privileges, and creates the simulation
    schema owned by the migrator.
    """
    engine = _superuser_engine(database, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(
                text(f'ALTER DATABASE "{database.name}" OWNER TO CURRENT_USER')
            )
            connection.execute(
                text(f"CREATE SCHEMA {SIMULATION_SCHEMA} AUTHORIZATION {MIGRATOR_ROLE}")
            )
            migrator_may_create = connection.execute(
                text(
                    "SELECT has_database_privilege(:role, current_database(), 'CREATE')"
                ),
                {"role": MIGRATOR_ROLE},
            ).scalar()
    finally:
        engine.dispose()
    assert migrator_may_create is False


def test_a_precreated_schema_needs_no_database_create_privilege(
    disposable_database,
) -> None:
    _precreate_schema_without_database_create(disposable_database)

    command.upgrade(alembic_config(), "head")

    assert _revision(disposable_database.migrator_url) == HEAD_REVISION
    assert _revision(disposable_database.runtime_url) == HEAD_REVISION


def test_the_runtime_role_reads_the_revision_but_cannot_change_the_schema(
    disposable_database,
) -> None:
    command.upgrade(alembic_config(), "head")

    assert _revision(disposable_database.runtime_url) == HEAD_REVISION
    engine = create_simulation_engine(disposable_database.runtime_url)
    try:
        with (
            pytest.raises(ProgrammingError, match="permission denied"),
            engine.begin() as connection,
        ):
            connection.execute(
                text("CREATE TABLE simulation.unowned (identifier integer)")
            )
        with (
            pytest.raises(ProgrammingError, match="permission denied"),
            engine.begin() as connection,
        ):
            connection.execute(
                text("UPDATE simulation.alembic_version SET version_num = ''")
            )
    finally:
        engine.dispose()


def test_a_failed_revision_leaves_nothing_applied(
    disposable_database, monkeypatch
) -> None:
    """The schema and every revision apply in one transaction."""
    from alembic.runtime import migration

    def fail_after_creating_the_schema(*_arguments, **_keywords):
        msg = "simulated failure inside the migration transaction"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        migration.MigrationContext,
        "run_migrations",
        fail_after_creating_the_schema,
    )

    with pytest.raises(RuntimeError, match="simulated failure"):
        command.upgrade(alembic_config(), "head")

    engine = _superuser_engine(disposable_database)
    try:
        with engine.connect() as connection:
            assert not inspect(connection).has_schema(SIMULATION_SCHEMA)
    finally:
        engine.dispose()
