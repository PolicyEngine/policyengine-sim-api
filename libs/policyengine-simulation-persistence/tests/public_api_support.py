"""The Public API tables the simulation schema refers to, reduced to their keys.

In production these tables belong to the Public API's own migration chain, in
the same database as the simulation schema. The tests create only the columns
and unique constraints the simulation schema's foreign keys use, with the
Public API's constraint names, owned by the superuser, and grant the migrator
``REFERENCES`` on them, as the operator does in production. The runtime role
gets no privilege on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from migration_support import MIGRATOR_ROLE
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from sqlalchemy import Engine

PUBLIC_API_TABLE_STATEMENTS = (
    "CREATE TABLE public.policies ("
    " id uuid PRIMARY KEY,"
    " country_id varchar(2) NOT NULL,"
    " CONSTRAINT uq_policies_id_country UNIQUE (id, country_id))",
    "CREATE TABLE public.households ("
    " id uuid PRIMARY KEY,"
    " country_id varchar(2) NOT NULL,"
    " CONSTRAINT uq_households_id_country UNIQUE (id, country_id))",
    "CREATE TABLE public.dynamics (id uuid PRIMARY KEY)",
)
REFERENCED_TABLES = ("policies", "households", "dynamics")


def superuser_engine(superuser_url: str, **options: object) -> Engine:
    """Return an engine that connects to the test database as the superuser."""
    return create_engine(
        superuser_url.replace("postgresql://", "postgresql+psycopg://", 1),
        **options,
    )


def create_public_api_tables(superuser_url: str, *, grant_references: bool) -> None:
    """Create the referenced Public API tables in a new test database."""
    engine = superuser_engine(superuser_url, isolation_level="AUTOCOMMIT")
    grant = (
        "GRANT REFERENCES ON "
        + ", ".join(f"public.{table}" for table in REFERENCED_TABLES)
        + f" TO {MIGRATOR_ROLE}"
    )
    statements = (
        (*PUBLIC_API_TABLE_STATEMENTS, grant)
        if grant_references
        else PUBLIC_API_TABLE_STATEMENTS
    )
    try:
        with engine.connect() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


@dataclass(frozen=True)
class PublicApiRows:
    """Public API rows a simulation can refer to."""

    us_policy_id: UUID
    uk_policy_id: UUID
    us_household_id: UUID
    uk_household_id: UUID
    dynamic_id: UUID


def insert_public_api_rows(superuser_url: str) -> PublicApiRows:
    """Insert rows into the referenced Public API tables, as their owner."""
    rows = PublicApiRows(
        us_policy_id=uuid4(),
        uk_policy_id=uuid4(),
        us_household_id=uuid4(),
        uk_household_id=uuid4(),
        dynamic_id=uuid4(),
    )
    statements = (
        (
            "INSERT INTO public.policies (id, country_id) VALUES "
            "(:us_policy, 'us'), (:uk_policy, 'uk')",
            {"us_policy": rows.us_policy_id, "uk_policy": rows.uk_policy_id},
        ),
        (
            "INSERT INTO public.households (id, country_id) VALUES "
            "(:us_household, 'us'), (:uk_household, 'uk')",
            {
                "us_household": rows.us_household_id,
                "uk_household": rows.uk_household_id,
            },
        ),
        (
            "INSERT INTO public.dynamics (id) VALUES (:dynamic)",
            {"dynamic": rows.dynamic_id},
        ),
    )
    engine = superuser_engine(superuser_url)
    try:
        with engine.begin() as connection:
            for statement, parameters in statements:
                connection.execute(text(statement), parameters)
    finally:
        engine.dispose()
    return rows


def delete_public_api_row(superuser_url: str, table_name: str, row_id: UUID) -> None:
    """Delete one Public API row as its owner, as the Public API would."""
    assert table_name in REFERENCED_TABLES
    engine = superuser_engine(superuser_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(f"DELETE FROM public.{table_name} WHERE id = :row_id"),  # noqa: S608  # table names come from REFERENCED_TABLES
                {"row_id": row_id},
            )
    finally:
        engine.dispose()
