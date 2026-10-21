"""The Public API tables that rows in the simulation schema refer to.

The Public API owns these tables and creates and changes them with its own
migration chain, in the same database as the ``simulation`` schema. They are
described here only so the simulation tables can declare foreign keys to them:
each lists just the key columns those foreign keys use, in a metadata object of
its own, so the simulation chain never compares, creates or changes them.

The tables have no schema here because PostgreSQL reports a foreign key into
``public`` without a schema when ``public`` is on the search path; naming one
would make every schema comparison report the foreign keys as changed. The
migrations name ``public`` explicitly.

Every foreign key into these tables is ``ON DELETE RESTRICT``: the Public API
cannot delete a row a simulation uses. Creating the foreign keys needs
``REFERENCES`` on each table for the migrator role; the runtime role needs no
privilege on them, because PostgreSQL runs a foreign key's check as the owner
of the referenced table.
"""

from __future__ import annotations

from sqlalchemy import Column, MetaData, String, Table, Uuid

public_api_metadata = MetaData()

policies = Table(
    "policies",
    public_api_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("country_id", String(2), nullable=False),
)
households = Table(
    "households",
    public_api_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("country_id", String(2), nullable=False),
)
dynamics = Table(
    "dynamics",
    public_api_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
)

REFERENCED_PUBLIC_API_TABLES = tuple(sorted(public_api_metadata.tables))
