"""Proof fixture environment: applies this chain to the migration database."""

import os

from alembic import context
from sqlalchemy import create_engine

url = os.environ["SIMULATION_MIGRATION_DATABASE_URL"].replace(
    "postgresql://", "postgresql+psycopg://", 1
)
engine = create_engine(url)
with engine.begin() as connection:
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table="proof_bad_chain_version",
    )
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
