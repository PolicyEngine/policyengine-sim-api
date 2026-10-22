"""Disposable PostgreSQL databases for the migration and store tests.

Tests marked ``postgres`` need a PostgreSQL server whose superuser URL is in
``SIMULATION_TEST_DATABASE_URL``. Each test gets a new database owned by a
non-superuser migrator role, so migrations run with the privileges they have in
production. Without the variable those tests skip, unless
``SIMULATION_REQUIRE_DATABASE_TESTS=1`` (set in CI), where they fail instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from alembic import command
from migration_support import (
    MIGRATION_DATABASE_URL_VARIABLE,
    MIGRATOR_ROLE,
    TEST_ROLE_PASSWORD,
    alembic_config,
)
from policyengine_simulation_persistence.schema import RUNTIME_ROLE
from public_api_support import (
    PublicApiRows,
    create_public_api_tables,
    insert_public_api_rows,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection

DATABASE_URL_VARIABLE = "SIMULATION_TEST_DATABASE_URL"
REQUIRE_DATABASE_VARIABLE = "SIMULATION_REQUIRE_DATABASE_TESTS"


@dataclass(frozen=True)
class DisposableDatabase:
    """One empty database and the URLs of the roles that use it."""

    name: str
    migrator_url: str
    runtime_url: str
    superuser_url: str


def _canonical(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _ensure_login_role(connection: Connection, role_name: str) -> None:
    connection.execute(
        text(
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role_name}') "
            f"THEN CREATE ROLE {role_name} LOGIN PASSWORD '{TEST_ROLE_PASSWORD}'; "
            "END IF; END $$"
        )
    )


@pytest.fixture(scope="session")
def postgres_server_url() -> URL:
    """Return the superuser URL of the test server, or skip without one."""
    configured = os.environ.get(DATABASE_URL_VARIABLE, "")
    if not configured:
        message = f"{DATABASE_URL_VARIABLE} names no PostgreSQL server"
        if os.environ.get(REQUIRE_DATABASE_VARIABLE) == "1":
            pytest.fail(message)
        pytest.skip(message)
    return make_url(configured).set(drivername="postgresql+psycopg")


@pytest.fixture
def references_granted() -> bool:
    """Whether the migrator gets REFERENCES on the Public API tables.

    The operator grants it in production; a test overrides this fixture to
    leave it out.
    """
    return True


@pytest.fixture
def disposable_database(
    postgres_server_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    references_granted,
) -> Iterator[DisposableDatabase]:
    """Create a database owned by the migrator, and drop it afterwards.

    It holds only the Public API tables the simulation schema refers to
    (``public_api_support``). ``SIMULATION_MIGRATION_DATABASE_URL`` points at
    it, as in the deploy job.
    """
    database_name = f"simulation_test_{uuid4().hex[:12]}"
    server_engine = create_engine(postgres_server_url, isolation_level="AUTOCOMMIT")
    try:
        with server_engine.connect() as connection:
            _ensure_login_role(connection, MIGRATOR_ROLE)
            _ensure_login_role(connection, RUNTIME_ROLE)
            connection.execute(
                text(f'CREATE DATABASE "{database_name}" OWNER {MIGRATOR_ROLE}')
            )
        database = DisposableDatabase(
            name=database_name,
            migrator_url=_canonical(
                postgres_server_url.set(
                    username=MIGRATOR_ROLE,
                    password=TEST_ROLE_PASSWORD,
                    database=database_name,
                )
            ),
            runtime_url=_canonical(
                postgres_server_url.set(
                    username=RUNTIME_ROLE,
                    password=TEST_ROLE_PASSWORD,
                    database=database_name,
                )
            ),
            superuser_url=_canonical(postgres_server_url.set(database=database_name)),
        )
        create_public_api_tables(
            database.superuser_url, grant_references=references_granted
        )
        monkeypatch.setenv(MIGRATION_DATABASE_URL_VARIABLE, database.migrator_url)
        yield database
    finally:
        with server_engine.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        server_engine.dispose()


@pytest.fixture
def migrated_database(disposable_database: DisposableDatabase) -> DisposableDatabase:
    """Return a disposable database with the whole chain applied."""
    command.upgrade(alembic_config(), "head")
    return disposable_database


@pytest.fixture
def public_api_rows(migrated_database: DisposableDatabase) -> PublicApiRows:
    """Insert one row of each kind the simulation tables refer to."""
    return insert_public_api_rows(migrated_database.superuser_url)
