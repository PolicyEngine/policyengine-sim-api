"""Proof fixture: household job records written on the SQLAlchemy ORM.

The rule must accept it. It is the bad fixture's records rewritten: the table
is a typed mapped class, every statement names the class or its attributes,
and each function runs on the ORM Session it is given. A partial index's
condition is the one place SQL text may appear, as a model's schema detail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar
from uuid import UUID  # noqa: TC003  # SQLAlchemy reads Mapped annotations at run time

from sqlalchemy import Index, String, func, select, text, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class Base(DeclarativeBase):
    """The declarative base every mapped class derives from."""


class HouseholdJob(Base):
    """One household calculation, as a row of household_jobs."""

    __tablename__ = "household_jobs"
    __table_args__: ClassVar = (
        Index(
            "ix_household_jobs_pending",
            "status",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String)
    modal_call_id: Mapped[str | None] = mapped_column(String)


def insert_household_job(session: Session, job_id: UUID) -> None:
    """Create the job ``pending``, with no Modal call ID."""
    session.add(HouseholdJob(id=job_id, status="pending", modal_call_id=None))
    session.flush()


def record_household_call(
    session: Session, *, job_id: UUID, modal_call_id: str
) -> str | None:
    """Record the job's Modal call ID while it is pending with none."""
    return session.execute(
        update(HouseholdJob)
        .where(HouseholdJob.id == job_id, HouseholdJob.modal_call_id.is_(None))
        .values(modal_call_id=modal_call_id)
        .returning(HouseholdJob.status)
    ).scalar_one_or_none()


def count_pending_jobs(session: Session) -> int:
    """Count pending jobs with a typed select on the mapped class."""
    return session.scalars(
        select(func.count())
        .select_from(HouseholdJob)
        .where(HouseholdJob.status == "pending")
    ).one()
