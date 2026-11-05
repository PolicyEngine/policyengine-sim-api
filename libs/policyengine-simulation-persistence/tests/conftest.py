"""PostgreSQL databases for the tests that need one.

Tests marked ``postgres`` need a PostgreSQL server whose superuser URL is in
``SIMULATION_TEST_DATABASE_URL``. Each test gets a new, empty database and a
login role that holds no privilege on it until the test grants one. Without
the variable those tests skip, unless ``SIMULATION_REQUIRE_DATABASE_TESTS=1``
(set in CI), where they fail instead.

Tests marked ``public_api_schema`` need a database the Public API's own v2
migration chain built, with the privileges its manifest declares granted
(run its ``alembic -c alembic-v2.ini upgrade head`` and
``scripts/reconcile_v2_privileges.py apply --create-missing-roles`` against
it). Its superuser URL is in ``PUBLIC_API_SCHEMA_DATABASE_URL``. Without it
those tests skip, unless ``SIMULATION_REQUIRE_PUBLIC_API_SCHEMA=1``.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from policyengine_simulation_persistence import create_simulation_engine
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection

DATABASE_URL_VARIABLE = "SIMULATION_TEST_DATABASE_URL"
REQUIRE_DATABASE_VARIABLE = "SIMULATION_REQUIRE_DATABASE_TESTS"
# A login role standing in for the simulation service's role.
SERVICE_TEST_ROLE = "simulation_service_test"
SERVICE_TEST_ROLE_PASSWORD = "simulation_service_test"
PUBLIC_API_DATABASE_VARIABLE = "PUBLIC_API_SCHEMA_DATABASE_URL"
REQUIRE_PUBLIC_API_VARIABLE = "SIMULATION_REQUIRE_PUBLIC_API_SCHEMA"
# The role the Public API's privilege manifest manages for the simulation
# service; the deployed SIMULATION_SERVICE_DATABASE_URL logs in as it.
SERVICE_ROLE = "policyengine_v2_simulation_service"


@dataclass(frozen=True)
class DisposableDatabase:
    """One empty database and the URLs of the roles that use it."""

    name: str
    superuser_url: str
    service_url: str


def _canonical(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


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
def disposable_database(postgres_server_url: URL) -> Iterator[DisposableDatabase]:
    """Create an empty database and the service's test role; drop the database after."""
    database_name = f"simulation_test_{uuid4().hex[:12]}"
    server_engine = create_engine(postgres_server_url, isolation_level="AUTOCOMMIT")
    try:
        with server_engine.connect() as connection:
            connection.execute(
                text(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = "
                    f"'{SERVICE_TEST_ROLE}') THEN CREATE ROLE {SERVICE_TEST_ROLE} "
                    f"LOGIN PASSWORD '{SERVICE_TEST_ROLE_PASSWORD}'; END IF; END $$"
                )
            )
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        yield DisposableDatabase(
            name=database_name,
            superuser_url=_canonical(postgres_server_url.set(database=database_name)),
            service_url=_canonical(
                postgres_server_url.set(
                    username=SERVICE_TEST_ROLE,
                    password=SERVICE_TEST_ROLE_PASSWORD,
                    database=database_name,
                )
            ),
        )
    finally:
        with server_engine.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        server_engine.dispose()


@pytest.fixture
def public_api_database_url() -> str:
    """Return the URL of the database the Public API's chain built, or skip."""
    configured = os.environ.get(PUBLIC_API_DATABASE_VARIABLE, "")
    if not configured:
        message = f"{PUBLIC_API_DATABASE_VARIABLE} names no Public API database"
        if os.environ.get(REQUIRE_PUBLIC_API_VARIABLE) == "1":
            pytest.fail(message)
        pytest.skip(message)
    return configured


@contextmanager
def connection_as_the_service(database_url: str) -> Iterator[Connection]:
    """Yield a connection acting as the service's role, rolled back afterwards.

    The manifest's roles may have no login in a test database, so the
    superuser connects and takes the role for the transaction alone.
    """
    engine = create_simulation_engine(database_url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text(f"SET LOCAL ROLE {SERVICE_ROLE}"))
                yield connection
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
