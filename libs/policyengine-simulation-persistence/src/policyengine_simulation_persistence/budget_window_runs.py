"""``budget_window_runs``: calculations of budget-window reports.

A budget-window report (``reports.budget_window_size``) compares its two
simulations in each year of its window. One budget-window run is one
calculation of it on a catalog version: it records what started it (its
trigger), whether its years calculate their simulations again (``force``), how
many years it calculates at once, its status and error, and when the safety
check next looks at it. Each year it calculates is one of its report runs
(``report_runs.budget_window_run_id``), at most one per year, of its report and
catalog version. The run stores no figures of its own: its totals are the sums
of its years' budget rows, added up when it is read.

A run is ``running`` until every year of its window has succeeded, when it is
``succeeded``, or until one year has failed, when it is ``failed`` with that
year's error. ``scheduling_claimed_until`` is the lease of the one caller
starting its next years at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)

from policyengine_simulation_persistence import public_api
from policyengine_simulation_persistence.reports import reports
from policyengine_simulation_persistence.runs import (
    ReportRunRecord,
    RunTrigger,
    SimulationRunRecord,
)
from policyengine_simulation_persistence.schema import simulation_metadata

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import RowMapping


class BudgetWindowStatus(StrEnum):
    """Where a budget-window run stands."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


FINISHED_BUDGET_WINDOW_STATUSES = (
    BudgetWindowStatus.SUCCEEDED,
    BudgetWindowStatus.FAILED,
)


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


budget_window_runs = Table(
    "budget_window_runs",
    simulation_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("report_id", Uuid(as_uuid=True), nullable=False),
    Column("tax_benefit_model_version_id", Uuid(as_uuid=True), nullable=False),
    Column("trigger", Text, nullable=False),
    Column("force", Boolean, nullable=False),
    Column("max_concurrent_years", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("error_code", Text, nullable=True),
    Column("error_message", Text, nullable=True),
    Column("next_check_at", DateTime(timezone=True), nullable=True),
    Column("scheduling_claimed_until", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(f"trigger IN ({_values(RunTrigger)})", name="trigger"),
    CheckConstraint(f"status IN ({_values(BudgetWindowStatus)})", name="status"),
    CheckConstraint("max_concurrent_years >= 1", name="max_concurrent_years"),
    # Only a running run is checked, and every running run is due a check.
    CheckConstraint(
        "(status = 'running') = (next_check_at IS NOT NULL)", name="next_check_at"
    ),
    CheckConstraint(
        "(status IN ('succeeded', 'failed')) = (completed_at IS NOT NULL)",
        name="completed_at",
    ),
    CheckConstraint("(status = 'failed') = (error_code IS NOT NULL)", name="error"),
    ForeignKeyConstraint(
        ["report_id"],
        [reports.c.id],
        name="fk_budget_window_runs_report",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tax_benefit_model_version_id"],
        [public_api.tax_benefit_model_versions.c.id],
        name="fk_budget_window_runs_catalog_version",
        ondelete="RESTRICT",
    ),
    # Each year's report run refers to the run with its report and catalog
    # version, so it is a run of the same report on the same version.
    UniqueConstraint(
        "id",
        "report_id",
        "tax_benefit_model_version_id",
        name="uq_budget_window_runs_id_report_catalog_version",
    ),
)
# A report's newest budget-window run is its current one.
Index(
    "ix_budget_window_runs_report_id_created_at",
    budget_window_runs.c.report_id,
    budget_window_runs.c.created_at,
)
# The safety check reads only running runs that are due a check.
Index(
    "ix_budget_window_runs_due_checks",
    budget_window_runs.c.next_check_at,
    postgresql_where=budget_window_runs.c.status == BudgetWindowStatus.RUNNING.value,
)
# A Public API delete of a catalog version checks this for references.
Index(
    "ix_budget_window_runs_tax_benefit_model_version_id",
    budget_window_runs.c.tax_benefit_model_version_id,
)


@dataclass(frozen=True)
class NewBudgetWindowRun:
    """A calculation of a budget-window report to create.

    ``force`` calculates every year's simulations again instead of reusing
    runs that exist; ``max_concurrent_years`` caps how many of its years are
    calculated at once.
    """

    report_id: UUID
    tax_benefit_model_version_id: UUID
    trigger: RunTrigger
    max_concurrent_years: int
    force: bool = False

    def __post_init__(self) -> None:
        """Reject a cap below one year without touching the database."""
        if self.max_concurrent_years < 1:
            msg = "max_concurrent_years must be at least 1"
            raise ValueError(msg)


@dataclass(frozen=True)
class BudgetWindowRunRecord:
    """One ``simulation.budget_window_runs`` row."""

    id: UUID
    report_id: UUID
    tax_benefit_model_version_id: UUID
    trigger: RunTrigger
    force: bool
    max_concurrent_years: int
    status: BudgetWindowStatus
    error_code: str | None
    error_message: str | None
    next_check_at: datetime | None
    scheduling_claimed_until: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def finished(self) -> bool:
        """Whether the run has succeeded or failed."""
        return self.status in FINISHED_BUDGET_WINDOW_STATUSES

    @classmethod
    def from_row(cls, row: RowMapping) -> Self:
        """Build the record from a selected ``budget_window_runs`` row."""
        values = {field.name: row[field.name] for field in fields(cls)}
        values["trigger"] = RunTrigger(values["trigger"])
        values["status"] = BudgetWindowStatus(values["status"])
        return cls(**values)


@dataclass(frozen=True)
class StartedBudgetWindowRun:
    """The run a first start resolved to, and whether this call created it."""

    run: BudgetWindowRunRecord
    created: bool


@dataclass(frozen=True)
class BudgetWindowYear:
    """One year of a budget-window run: its report run and both simulation runs."""

    report_run: ReportRunRecord
    baseline_run: SimulationRunRecord
    reform_run: SimulationRunRecord


@dataclass(frozen=True)
class YearBudget:
    """A year's budget figures, from its comparison's ``budget_impacts`` row."""

    year: int
    budgetary_impact: float
    tax_revenue_impact: float
    state_tax_revenue_impact: float
    benefit_spending_impact: float
