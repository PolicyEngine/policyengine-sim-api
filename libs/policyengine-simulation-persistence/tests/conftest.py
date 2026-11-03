"""Disposable PostgreSQL databases for the tests that need one.

Tests marked ``postgres`` need a PostgreSQL server whose superuser URL is in
``SIMULATION_TEST_DATABASE_URL``. Each test gets a new, empty database and a
login role that holds no privilege on it until the test grants one. Without
the variable those tests skip, unless ``SIMULATION_REQUIRE_DATABASE_TESTS=1``
(set in CI), where they fail instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

if TYPE_CHECKING:
    from collections.abc import Iterator

DATABASE_URL_VARIABLE = "SIMULATION_TEST_DATABASE_URL"
REQUIRE_DATABASE_VARIABLE = "SIMULATION_REQUIRE_DATABASE_TESTS"
# A login role standing in for the simulation service's role.
SERVICE_TEST_ROLE = "simulation_service_test"
SERVICE_TEST_ROLE_PASSWORD = "simulation_service_test"


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
