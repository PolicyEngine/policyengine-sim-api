"""The model of ``cliff_impacts``: each simulation run's cliff calculations.

A simulation run can have one cliff result, its cliff gap and cliff share,
which every report naming the run reads. Each row is one calculation of it:
the simulation service inserts it pending and records the Modal call it
starts, and the Modal worker role claims it under that call and records the
two numbers or the failure. A run has at most one calculation that hasn't
failed, so identical requests join it; a failed one stays recorded, and a
later start inserts a new row. The row has no versions of its own: it is a
calculation of its run, on the run's versions. Deleting a run deletes its
calculations.

The Public API's v2 migration chain creates the table (revision
``bc334f9877b7``); this class only describes it, column for column, with its
constraints and indexes under the same names. It is one of the service's
tables (``ServiceBase``), since the service creates its rows.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # SQLAlchemy reads Mapped annotations
from typing import ClassVar
from uuid import UUID  # noqa: TC003  # SQLAlchemy reads Mapped annotations at run time

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from policyengine_simulation_persistence.orm_base import (
    ServiceBase,
    enum_type,
    identifier_column,
    optional_timestamp_column,
    server_timestamp_column,
)
from policyengine_simulation_persistence.tables import (
    MODAL_CALL_ID_LENGTH,
    SimulationRun,
    SimulationStatus,
)


class CliffImpact(ServiceBase):
    """One cliff calculation of a simulation run, and its result once it succeeds."""

    __tablename__ = "cliff_impacts"
    __table_args__: ClassVar = (
        PrimaryKeyConstraint("id", name="pk_cliff_impacts"),
        ForeignKeyConstraint(
            ["simulation_run_id"],
            [SimulationRun.id],
            name="fk_cliff_impacts_simulation_run_id_simulation_runs",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status NOT IN ('succeeded', 'failed') OR completed_at IS NOT NULL",
            name="ck_cliff_impacts_finished_completion",
        ),
        CheckConstraint(
            "(cliff_gap IS NULL) = (cliff_share IS NULL) AND "
            "(status = 'succeeded') = (cliff_gap IS NOT NULL)",
            name="ck_cliff_impacts_result_when_succeeded",
        ),
        Index("ix_cliff_impacts_simulation_run_id", "simulation_run_id"),
        # For the stuck-run sweep, which judges a calculation by its age.
        Index("ix_cliff_impacts_status_started_at", "status", "started_at"),
        # One calculation per run that hasn't failed: find-or-create's conflict
        # target, so identical requests join one calculation.
        Index(
            "uq_cliff_impacts_unfailed",
            "simulation_run_id",
            unique=True,
            postgresql_where=text("status <> 'failed'"),
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    updated_at: Mapped[datetime] = server_timestamp_column()
    simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    # A cliff calculation has a simulation run's states, under its type.
    status: Mapped[SimulationStatus] = mapped_column(
        enum_type(SimulationStatus, "v2_simulation_status")
    )
    modal_call_id: Mapped[str | None] = mapped_column(String(MODAL_CALL_ID_LENGTH))
    error_message: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime | None] = optional_timestamp_column()
    completed_at: Mapped[datetime | None] = optional_timestamp_column()
    # Both set exactly when the calculation succeeded.
    cliff_gap: Mapped[float | None] = mapped_column(Float)
    cliff_share: Mapped[float | None] = mapped_column(Float)
