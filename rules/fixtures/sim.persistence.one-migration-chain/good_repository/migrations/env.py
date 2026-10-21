"""Proof fixture: an Alembic environment that versions the simulation schema."""

from alembic import context

SIMULATION_SCHEMA = "simulation"

context.configure(version_table_schema=SIMULATION_SCHEMA)
