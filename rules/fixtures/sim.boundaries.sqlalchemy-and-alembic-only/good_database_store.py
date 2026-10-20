"""Proof fixture: a store that connects through SQLAlchemy and only reads rows.

The rule must accept it. It follows today's Stage 12 store: a SQLAlchemy engine
selects the psycopg 3 driver through the URL's driver name, and statements are
typed SQLAlchemy Core. It never runs CREATE TABLE or metadata.create_all; the
schema changes only through Alembic migrations. Dropping a pandas column is not
a schema change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
from sqlalchemy import Column, MetaData, String, Table, create_engine, select
from sqlalchemy.engine import make_url

if TYPE_CHECKING:
    from sqlalchemy import Engine

metadata = MetaData()
comparison_reports = Table(
    "stage12_evaluation_reports",
    metadata,
    Column("evaluation_id", String, primary_key=True),
    Column("status", String),
)


def create_store_engine(database_url: str) -> Engine:
    """Build an engine that connects with psycopg 3 through SQLAlchemy."""
    return create_engine(make_url(database_url).set(drivername="postgresql+psycopg"))


def report_statuses(engine: Engine) -> pd.DataFrame:
    """Read report statuses with a typed select, without the identifier column."""
    with engine.connect() as connection:
        rows = connection.execute(select(comparison_reports)).mappings().all()
    frame = pd.DataFrame(rows)
    return frame.drop(columns=["evaluation_id"])
