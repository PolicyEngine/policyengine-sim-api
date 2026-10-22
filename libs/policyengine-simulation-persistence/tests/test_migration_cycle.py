"""Run the simulation migration chain against a fresh PostgreSQL database."""

from __future__ import annotations

import re
import time

import pytest
from alembic import command
from migration_support import (
    MIGRATOR_ROLE,
    REPOSITORY_ROOT,
    alembic_config,
    applied_revision,
    chain_config,
    migration_cycle_failures,
)
from policyengine_simulation_persistence import (
    HEAD_REVISION,
    SIMULATION_SCHEMA,
    create_simulation_engine,
    simulation_metadata,
)
from report_support import economy_pair, economy_report, report_store
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.schema import CreateTable

pytestmark = pytest.mark.postgres


def _superuser_engine(database, **options):
    return create_engine(
        database.superuser_url.replace("postgresql://", "postgresql+psycopg://", 1),
        **options,
    )


def test_upgrade_check_downgrade_upgrade_cycle(disposable_database) -> None:
    config = alembic_config()

    assert migration_cycle_failures(config, check=True) == []
    assert applied_revision(disposable_database.migrator_url) == HEAD_REVISION

    command.downgrade(config, "base")
    assert applied_revision(disposable_database.migrator_url) is None


def test_rule_proof_migration_cycle(disposable_database) -> None:
    """Proof for rule sim.persistence.migration-cycle (rules/registry.toml)."""
    fixtures = (
        REPOSITORY_ROOT / "rules" / "fixtures" / "sim.persistence.migration-cycle"
    )

    assert (
        migration_cycle_failures(chain_config(fixtures / "good_chain"), check=False)
        == []
    )
    assert migration_cycle_failures(
        chain_config(fixtures / "bad_chain"), check=False
    ) == [
        "upgrade failed: ProgrammingError: (psycopg.errors.DuplicateTable) relation "
        '"proof_bad_table" already exists'
    ]


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
    assert applied_revision(disposable_database.migrator_url) is None


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

    assert applied_revision(disposable_database.migrator_url) == HEAD_REVISION
    assert applied_revision(disposable_database.runtime_url) == HEAD_REVISION


def test_the_runtime_role_reads_the_revision_but_cannot_change_the_schema(
    disposable_database,
) -> None:
    command.upgrade(alembic_config(), "head")

    assert applied_revision(disposable_database.runtime_url) == HEAD_REVISION
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


def test_a_migration_waiting_for_a_lock_gives_up_instead_of_blocking(
    disposable_database,
) -> None:
    """Writers queue behind a waiting migration, so it must not wait long."""
    command.upgrade(alembic_config(), "head")
    holder = _superuser_engine(disposable_database)
    try:
        with holder.begin() as connection:
            connection.execute(
                text("LOCK TABLE simulation.alembic_version IN ACCESS EXCLUSIVE MODE")
            )
            started = time.monotonic()
            with pytest.raises(OperationalError, match="lock timeout"):
                command.upgrade(alembic_config(), "head")
            waited = time.monotonic() - started
    finally:
        holder.dispose()
    assert waited < 30


@pytest.mark.parametrize("references_granted", [False])
def test_the_chain_needs_references_on_the_public_api_tables(
    disposable_database,
) -> None:
    """Without the operator's REFERENCES grant, no foreign key can be created."""
    with pytest.raises(ProgrammingError, match="permission denied for table"):
        command.upgrade(alembic_config(), "head")

    assert applied_revision(disposable_database.migrator_url) is None


@pytest.mark.parametrize("table_name", ["simulations", "reports"])
def test_the_runtime_role_cannot_change_or_delete_a_definition(
    migrated_database, public_api_rows, table_name
) -> None:
    baseline, reform = economy_pair(migrated_database, public_api_rows)
    report_store(migrated_database).create(economy_report(baseline, reform))
    engine = create_simulation_engine(migrated_database.runtime_url)
    try:
        for statement in (
            f"UPDATE simulation.{table_name} SET created_at = now()",  # noqa: S608  # fixed table names
            f"DELETE FROM simulation.{table_name}",  # noqa: S608  # fixed table names
        ):
            with (
                pytest.raises(ProgrammingError, match="permission denied"),
                engine.begin() as connection,
            ):
                connection.execute(text(statement))
    finally:
        engine.dispose()


def _declared_constraint_names() -> set[str]:
    """Return the names the table definitions give their constraints."""
    names: set[str] = set()
    for table in simulation_metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        names.update(re.findall(r"CONSTRAINT (\w+)", ddl))
    return names


def test_every_constraint_has_the_name_its_definition_gives_it(
    migrated_database,
) -> None:
    """Compare names here, since alembic check does not compare check constraints."""
    engine = _superuser_engine(migrated_database)
    try:
        with engine.connect() as connection:
            database_names = set(
                connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "JOIN pg_namespace ON pg_namespace.oid = connamespace "
                        "WHERE nspname = 'simulation' "
                        "AND contype IN ('c', 'f', 'p', 'u') "
                        "AND conrelid <> 'simulation.alembic_version'::regclass"
                    )
                ).scalars()
            )
    finally:
        engine.dispose()

    assert database_names == _declared_constraint_names()
