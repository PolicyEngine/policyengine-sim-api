"""Proof fixture: household job records written on SQLAlchemy Core.

The rule must reject it. The table, the insert and the call recording are the
Core code policyengine-simulation-persistence had when the rule was added,
shortened: household_jobs was a Core Table, written through a Connection. The
engine factory imports create_engine outside a library's engine.py, and the
count runs SQL text; both are illustrative, not taken from the library.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Column, MetaData, String, Table, Uuid, insert, text, update
from sqlalchemy import create_engine as build_engine

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.engine import Connection, Engine

service_metadata = MetaData(schema="public")
household_jobs = Table(
    "household_jobs",
    service_metadata,
    Column("id", Uuid, primary_key=True),
    Column("status", String, nullable=False),
    Column("modal_call_id", String, nullable=True),
)


def create_household_engine(database_url: str) -> Engine:
    """Build an engine outside the library's session factory."""
    return build_engine(database_url)


def insert_household_job(connection: Connection, job_id: UUID) -> None:
    """Create the job ``pending``, with no Modal call ID."""
    connection.execute(
        insert(household_jobs).values(id=job_id, status="pending", modal_call_id=None)
    )


def record_household_call(
    connection: Connection, *, job_id: UUID, modal_call_id: str
) -> str | None:
    """Record the job's Modal call ID while it is pending with none."""
    return connection.execute(
        update(household_jobs)
        .where(
            household_jobs.c.id == job_id,
            household_jobs.c.modal_call_id.is_(None),
        )
        .values(modal_call_id=modal_call_id)
        .returning(household_jobs.c.status)
    ).scalar_one_or_none()


def count_pending_jobs(engine: Engine) -> int:
    """Count pending jobs with SQL text on a connection of its own."""
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT count(*) FROM household_jobs WHERE status = 'pending'")
        ).scalar_one()
