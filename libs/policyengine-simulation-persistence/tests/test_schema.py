"""Schema names and connection settings, without a database."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from migration_support import MIGRATION_DATABASE_URL_VARIABLE, alembic_config
from policyengine_simulation_persistence import (
    HEAD_REVISION,
    RUNTIME_ROLE,
    SIMULATION_SCHEMA,
    create_simulation_engine,
    normalized_database_url,
    simulation_metadata,
)


def test_the_schema_and_runtime_role_have_their_deployed_names() -> None:
    assert SIMULATION_SCHEMA == "simulation"
    assert RUNTIME_ROLE == "simulation_runtime"


def test_every_table_is_in_the_simulation_schema() -> None:
    assert simulation_metadata.schema == SIMULATION_SCHEMA
    for table in simulation_metadata.tables.values():
        assert table.schema == SIMULATION_SCHEMA, table.name


def test_database_url_selects_psycopg_without_changing_the_secret_format() -> None:
    url = normalized_database_url(
        "postgresql://simulation_runtime:secret@db.example:5432/postgres"
    )

    assert url.drivername == "postgresql+psycopg"
    assert url.username == "simulation_runtime"
    assert url.host == "db.example"
    assert url.database == "postgres"


@pytest.mark.parametrize(
    "database_url",
    [
        "",
        " postgresql://user:secret@db.example/postgres",
        "postgres://user:secret@db.example/postgres",
        "postgresql+psycopg://user:secret@db.example/postgres",
        "postgresql://db.example/postgres",
        "postgresql://user:secret@db.example",
        "not a url",
    ],
)
def test_malformed_database_urls_are_rejected_without_echoing_them(
    database_url,
) -> None:
    with pytest.raises(ValueError, match="Simulation database URL") as error:
        normalized_database_url(database_url)

    assert "secret" not in str(error.value)
    assert error.value.__cause__ is None or "secret" not in str(error.value.__cause__)


def test_runtime_engine_never_prepares_server_side_statements(monkeypatch) -> None:
    captured_options = {}

    def capture_engine(url, **options):
        captured_options.update(options, url=url)
        return object()

    monkeypatch.setattr(
        "policyengine_simulation_persistence.engine.create_engine",
        capture_engine,
    )

    create_simulation_engine("postgresql://runtime:secret@db.example/postgres")

    assert captured_options["url"].drivername == "postgresql+psycopg"
    assert captured_options["connect_args"]["prepare_threshold"] is None
    assert captured_options["connect_args"]["connect_timeout"] > 0
    assert captured_options["pool_pre_ping"] is True
    assert captured_options["pool_size"] >= 1


def test_head_revision_names_the_single_head_of_the_chain() -> None:
    script = ScriptDirectory.from_config(alembic_config())

    assert script.get_heads() == [HEAD_REVISION]


def test_migrations_refuse_to_run_without_the_migration_database_url(
    monkeypatch,
) -> None:
    monkeypatch.delenv(MIGRATION_DATABASE_URL_VARIABLE, raising=False)

    with pytest.raises(RuntimeError, match=MIGRATION_DATABASE_URL_VARIABLE):
        command.upgrade(alembic_config(), "head")


def test_migrations_refuse_offline_sql_generation(monkeypatch) -> None:
    monkeypatch.setenv(
        MIGRATION_DATABASE_URL_VARIABLE,
        "postgresql://simulation_migrator:secret@db.example/postgres",
    )

    with pytest.raises(RuntimeError, match="online"):
        command.upgrade(alembic_config(), "head", sql=True)
