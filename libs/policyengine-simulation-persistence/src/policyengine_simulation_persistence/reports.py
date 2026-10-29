"""``simulation.reports``: which two simulations a report compares.

A report is an economy or household comparison a user can save: its baseline
simulation, its reform simulation (required for an economy report; a household
report may have only a baseline), the year and further options. It is a
definition and never changes. Both simulations must have the report's country
and kind, which composite foreign keys into ``simulation.simulations`` check.

A report's calculations are its report runs (item 1.06); a report with none has
not been calculated yet. A report migrated from the Public API's v1 database
keeps its v1 report ID in ``legacy_report_id``.

A budget-window report is an economy report with a ``budget_window_size``: it
compares its two simulations in each of that many years, starting with its
``year``. Each of its calculations is a budget-window run
(``budget_window_runs``), which holds one report run per year.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from dataclasses import field as dataclass_field
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
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
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from policyengine_simulation_persistence.schema import simulation_metadata
from policyengine_simulation_persistence.simulations import (
    check_country,
    check_legacy_id,
    insert_or_get_existing,
    parse_kind,
    simulations,
)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import ColumnElement, Engine, RowMapping

FIRST_YEAR = 1900
LAST_YEAR = 2200
# The most years one budget-window report compares.
MAX_BUDGET_WINDOW_YEARS = 75


class ReportKind(StrEnum):
    """What a report compares; its simulations have the same kind."""

    ECONOMY = "economy"
    HOUSEHOLD = "household"


_KIND_VALUES = ", ".join(f"'{kind.value}'" for kind in ReportKind)
_SIMULATION_KEY = [simulations.c.id, simulations.c.country, simulations.c.kind]

reports = Table(
    "reports",
    simulation_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("country", Text, nullable=False),
    Column("baseline_simulation_id", Uuid(as_uuid=True), nullable=False),
    Column("reform_simulation_id", Uuid(as_uuid=True), nullable=True),
    Column("year", Integer, nullable=False),
    Column("options", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("legacy_report_id", BigInteger, nullable=True, unique=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("budget_window_size", Integer, nullable=True),
    CheckConstraint(f"kind IN ({_KIND_VALUES})", name="kind"),
    CheckConstraint("country ~ '^[a-z]{2}$'", name="country"),
    CheckConstraint(f"year BETWEEN {FIRST_YEAR} AND {LAST_YEAR}", name="year"),
    CheckConstraint("jsonb_typeof(options) = 'object'", name="options"),
    CheckConstraint(
        "kind = 'household' OR reform_simulation_id IS NOT NULL",
        name="economy_has_reform",
    ),
    CheckConstraint(
        "legacy_report_id IS NULL OR legacy_report_id > 0",
        name="legacy_report_id",
    ),
    # Only an economy report has a budget window, and its last year is a year
    # a run may have.
    CheckConstraint(
        "budget_window_size IS NULL OR (kind = 'economy' "
        f"AND budget_window_size BETWEEN 1 AND {MAX_BUDGET_WINDOW_YEARS} "
        f"AND year + budget_window_size - 1 <= {LAST_YEAR})",
        name="budget_window_size",
    ),
    ForeignKeyConstraint(
        ["baseline_simulation_id", "country", "kind"],
        _SIMULATION_KEY,
        name="fk_reports_baseline_simulation",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["reform_simulation_id", "country", "kind"],
        _SIMULATION_KEY,
        name="fk_reports_reform_simulation",
        ondelete="RESTRICT",
    ),
    # Report runs refer to a report together with its kind, so only an economy
    # report run has a comparison step.
    UniqueConstraint("id", "kind"),
)
Index("ix_reports_baseline_simulation_id", reports.c.baseline_simulation_id)
Index("ix_reports_reform_simulation_id", reports.c.reform_simulation_id)


@dataclass(frozen=True)
class NewReport:
    """A report to create.

    The database checks that both simulations exist with the report's country
    and kind, and that an economy report has a reform; the fields are checked
    here first.
    """

    kind: ReportKind
    country: str
    baseline_simulation_id: UUID
    year: int
    reform_simulation_id: UUID | None = None
    options: dict[str, Any] = dataclass_field(default_factory=dict)
    legacy_report_id: int | None = None
    budget_window_size: int | None = None

    def __post_init__(self) -> None:
        """Reject malformed fields without touching the database."""
        # A frozen dataclass sets its own field through object.__setattr__.
        object.__setattr__(self, "kind", parse_kind(self.kind, ReportKind))
        check_country(self.country)
        if not FIRST_YEAR <= self.year <= LAST_YEAR:
            msg = f"year must be between {FIRST_YEAR} and {LAST_YEAR}"
            raise ValueError(msg)
        if not isinstance(self.options, dict):
            msg = "options must be a JSON object"
            raise TypeError(msg)
        check_legacy_id("legacy_report_id", self.legacy_report_id)
        self._check_budget_window()

    def _check_budget_window(self) -> None:
        size = self.budget_window_size
        if size is None:
            return
        if self.kind is not ReportKind.ECONOMY:
            msg = "only an economy report has a budget window"
            raise ValueError(msg)
        if not 1 <= size <= MAX_BUDGET_WINDOW_YEARS:
            msg = f"budget_window_size must be between 1 and {MAX_BUDGET_WINDOW_YEARS}"
            raise ValueError(msg)
        if self.year + size - 1 > LAST_YEAR:
            msg = f"a budget window must end in {LAST_YEAR} or earlier"
            raise ValueError(msg)


@dataclass(frozen=True)
class ReportRecord:
    """One ``simulation.reports`` row."""

    id: UUID
    kind: ReportKind
    country: str
    baseline_simulation_id: UUID
    reform_simulation_id: UUID | None
    year: int
    options: dict[str, Any]
    legacy_report_id: int | None
    created_at: datetime
    budget_window_size: int | None = None

    @property
    def window_years(self) -> range:
        """Return the years the report compares: its year, or its budget window."""
        return range(self.year, self.year + (self.budget_window_size or 1))

    @classmethod
    def from_row(cls, row: RowMapping) -> ReportRecord:
        """Build the record from a selected ``reports`` row."""
        values = {field.name: row[field.name] for field in fields(cls)}
        values["kind"] = ReportKind(values["kind"])
        return cls(**values)


def _new_report_values(new_report: NewReport) -> dict[str, Any]:
    return {
        "id": uuid4(),
        **{field.name: getattr(new_report, field.name) for field in fields(new_report)},
        "kind": new_report.kind.value,
    }


class ReportStore:
    """Typed SQLAlchemy Core reads and inserts for report rows."""

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, which connects as the runtime role."""
        self.engine = engine

    def create(self, new_report: NewReport) -> ReportRecord:
        """Insert one report and return it."""
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    postgres_insert(reports)
                    .values(**_new_report_values(new_report))
                    .returning(*reports.c)
                )
                .mappings()
                .one()
            )
        return ReportRecord.from_row(row)

    def create_migrated(self, new_report: NewReport) -> ReportRecord:
        """Create a report for a v1 report, or return the existing one.

        The v1 report ID must be set. An existing row is returned as it is,
        even if ``new_report`` describes it differently, so running the
        one-time migration again creates nothing new. The report has no run
        until one is added.
        """
        if new_report.legacy_report_id is None:
            msg = "a migrated report needs its legacy_report_id"
            raise ValueError(msg)
        with self.engine.begin() as connection:
            row = insert_or_get_existing(
                connection,
                reports,
                _new_report_values(new_report),
                reports.c.legacy_report_id,
            )
        return ReportRecord.from_row(row)

    def get(self, report_id: UUID) -> ReportRecord | None:
        """Return the report with this ID, or None."""
        return self._one(reports.c.id == report_id)

    def get_by_legacy_report_id(self, legacy_report_id: int) -> ReportRecord | None:
        """Return the report migrated from this v1 report, or None."""
        return self._one(reports.c.legacy_report_id == legacy_report_id)

    def _one(self, condition: ColumnElement[bool]) -> ReportRecord | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(select(reports).where(condition)).mappings().first()
            )
        return None if row is None else ReportRecord.from_row(row)
