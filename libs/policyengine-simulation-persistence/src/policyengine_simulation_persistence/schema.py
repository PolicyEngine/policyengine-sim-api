"""Names and metadata of the Simulation Entrypoint's own Postgres schema.

The Simulation Entrypoint owns every table in the ``simulation`` schema. One
Alembic chain, at the repository's ``migrations/`` directory, creates and
changes them, and records its revision in ``simulation.alembic_version``.
"""

from __future__ import annotations

from sqlalchemy import Column, MetaData, String, Table

SIMULATION_SCHEMA = "simulation"
ALEMBIC_VERSION_TABLE = "alembic_version"
# The login role the Simulation Entrypoint runtime connects as. Migrations grant
# it schema usage and table DML; it never owns or changes schema objects.
RUNTIME_ROLE = "simulation_runtime"
# The newest revision in migrations/versions. Each migration PR updates it, and
# tests/test_schema.py fails when it differs from the chain's head. The
# readiness check reports it next to the database's revision.
HEAD_REVISION = "0001"

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Every table the Simulation Entrypoint owns is registered here.
simulation_metadata = MetaData(
    schema=SIMULATION_SCHEMA,
    naming_convention=NAMING_CONVENTION,
)

# Alembic creates and owns this table; it is described here, outside
# simulation_metadata, only so the runtime can read the applied revision
# without depending on Alembic.
_alembic_metadata = MetaData(schema=SIMULATION_SCHEMA)
alembic_version = Table(
    ALEMBIC_VERSION_TABLE,
    _alembic_metadata,
    Column("version_num", String(32), primary_key=True),
)
