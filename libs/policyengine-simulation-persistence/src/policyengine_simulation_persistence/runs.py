"""``simulation_runs`` and ``report_runs``: executions of simulations and reports.

A simulation run is one execution of a simulation for a catalog version and a
year. It records the region and dataset rows it resolved in that catalog
version, its own status and Modal call, its timings and error, what started it
(its trigger) and its attempt number. Runs are unique per simulation, catalog
version, year and attempt, so a run that already exists is reused, and a forced
re-run adds the next attempt.

A report run is one calculation of a report: a baseline and a reform
simulation run of the same catalog version and year (the reform is null for a
single-simulation household report), plus, for an economy report, the
comparison step's own status and Modal call. A household report has no
comparison step: one Modal call computes both households, so its simulation
runs share that call.

Each status moves ``pending`` -> ``dispatching`` -> ``running`` -> ``succeeded``
or ``failed``. The region and dataset keys include the catalog version, through
the Public API's unique constraints ``uq_regions_model_version_code`` and
``uq_datasets_id_model_version``, so a run can use only its own version's
rows. A report run's simulation run keys include its catalog version and year,
so both runs share them.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Self

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from policyengine_simulation_persistence import public_api
from policyengine_simulation_persistence.reports import (
    FIRST_YEAR,
    LAST_YEAR,
    ReportKind,
    reports,
)
from policyengine_simulation_persistence.schema import simulation_metadata
from policyengine_simulation_persistence.simulations import (
    CATALOG_KEY_LENGTH,
    SimulationKind,
    simulations,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import RowMapping

MAX_ERROR_MESSAGE_LENGTH = 2000


class RunStatus(StrEnum):
    """Where a simulation run, or a report run's comparison step, stands."""

    PENDING = "pending"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


UNFINISHED_STATUSES = (RunStatus.PENDING, RunStatus.DISPATCHING, RunStatus.RUNNING)
FINISHED_STATUSES = (RunStatus.SUCCEEDED, RunStatus.FAILED)


class RunTrigger(StrEnum):
    """What started a run.

    ``initial`` is a report's or calculation's first submission, ``manual`` a
    user's re-run, and ``system`` a run sim or a migration started itself, for
    example the first opening of a migrated report.
    """

    INITIAL = "initial"
    MANUAL = "manual"
    SYSTEM = "system"


def _values(enum: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)


def _status_checks(prefix: str, *, optional: bool) -> tuple[CheckConstraint, ...]:
    """Return the checks that keep one status and its columns consistent.

    ``prefix`` is empty for a simulation run and ``comparison_`` for a report
    run's comparison step, whose status is null (``optional``) when the report
    run has no comparison step.
    """
    status = f"{prefix}status"
    unless_absent = f"{status} IS NULL OR " if optional else ""
    return (
        CheckConstraint(
            f"{unless_absent}{status} IN ({_values(RunStatus)})",
            name=f"{prefix}status",
        ),
        # A Modal call exists once dispatching succeeded, never before; a
        # failed run may have one or not.
        CheckConstraint(
            f"{unless_absent}"
            f"({status} IN ('pending', 'dispatching') "
            f"AND {prefix}modal_call_id IS NULL) "
            f"OR ({status} IN ('running', 'succeeded') "
            f"AND {prefix}modal_call_id IS NOT NULL) "
            f"OR {status} = 'failed'",
            name=f"{prefix}modal_call",
        ),
        CheckConstraint(
            f"{unless_absent}"
            f"({status} IN ('succeeded', 'failed')) "
            f"= ({prefix}completed_at IS NOT NULL)",
            name=f"{prefix}completed_at",
        ),
        CheckConstraint(
            f"{unless_absent}({status} = 'failed') = ({prefix}error_code IS NOT NULL)",
            name=f"{prefix}error",
        ),
        # Only a running call is checked, and every running call is due a check.
        CheckConstraint(
            f"{unless_absent}"
            f"({status} = 'running') = ({prefix}next_check_at IS NOT NULL)",
            name=f"{prefix}next_check_at",
        ),
        CheckConstraint(f"{prefix}check_count >= 0", name=f"{prefix}check_count"),
    )


def _execution_columns(prefix: str, *, required: bool) -> list[Column[Any]]:
    """Return the status, Modal call, error and timing columns of one execution."""
    return [
        Column(f"{prefix}status", Text, nullable=not required),
        Column(f"{prefix}modal_call_id", Text, nullable=True),
        Column(f"{prefix}error_code", Text, nullable=True),
        Column(f"{prefix}error_message", Text, nullable=True),
        Column(
            f"{prefix}check_count",
            Integer,
            nullable=False,
            server_default=text("0"),
        ),
        Column(f"{prefix}dispatched_at", DateTime(timezone=True), nullable=True),
        Column(f"{prefix}next_check_at", DateTime(timezone=True), nullable=True),
        Column(f"{prefix}completed_at", DateTime(timezone=True), nullable=True),
    ]


simulation_runs = Table(
    "simulation_runs",
    simulation_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("simulation_id", Uuid(as_uuid=True), nullable=False),
    Column("simulation_kind", Text, nullable=False),
    Column("tax_benefit_model_version_id", Uuid(as_uuid=True), nullable=False),
    Column("year", Integer, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("region_code", String(CATALOG_KEY_LENGTH), nullable=True),
    Column("dataset_id", Uuid(as_uuid=True), nullable=True),
    Column("trigger", Text, nullable=False),
    *_execution_columns("", required=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(f"year BETWEEN {FIRST_YEAR} AND {LAST_YEAR}", name="year"),
    CheckConstraint("attempt >= 1", name="attempt"),
    CheckConstraint(f"trigger IN ({_values(RunTrigger)})", name="trigger"),
    CheckConstraint(
        "(simulation_kind = 'household' AND region_code IS NULL "
        "AND dataset_id IS NULL) "
        "OR (simulation_kind = 'economy' AND region_code IS NOT NULL "
        "AND dataset_id IS NOT NULL)",
        name="population_matches_kind",
    ),
    *_status_checks("", optional=False),
    UniqueConstraint(
        "simulation_id",
        "tax_benefit_model_version_id",
        "year",
        "attempt",
        name="uq_simulation_runs_attempt",
    ),
    # Report runs refer to a simulation run with its catalog version and year,
    # so both runs of a report run share them.
    UniqueConstraint(
        "id",
        "tax_benefit_model_version_id",
        "year",
        name="uq_simulation_runs_id_catalog_version_year",
    ),
    ForeignKeyConstraint(
        ["simulation_id", "simulation_kind"],
        [simulations.c.id, simulations.c.kind],
        name="fk_simulation_runs_simulation",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tax_benefit_model_version_id"],
        [public_api.tax_benefit_model_versions.c.id],
        name="fk_simulation_runs_catalog_version",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["tax_benefit_model_version_id", "region_code"],
        [
            public_api.regions.c.tax_benefit_model_version_id,
            public_api.regions.c.code,
        ],
        name="fk_simulation_runs_region",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["dataset_id", "tax_benefit_model_version_id"],
        [
            public_api.datasets.c.id,
            public_api.datasets.c.tax_benefit_model_version_id,
        ],
        name="fk_simulation_runs_dataset",
        ondelete="RESTRICT",
    ),
)
# A Public API delete of a catalog version, region or dataset checks these for
# references.
Index(
    "ix_simulation_runs_catalog_version_region",
    simulation_runs.c.tax_benefit_model_version_id,
    simulation_runs.c.region_code,
)
Index("ix_simulation_runs_dataset_id", simulation_runs.c.dataset_id)
# Every run of one Modal call (a household report's two runs share one).
Index(
    "ix_simulation_runs_modal_call_id",
    simulation_runs.c.modal_call_id,
    postgresql_where=simulation_runs.c.modal_call_id.is_not(None),
)
# A recording pass reads only running runs that are due a check, and
# dispatches that stopped.
Index(
    "ix_simulation_runs_due_checks",
    simulation_runs.c.next_check_at,
    postgresql_where=simulation_runs.c.status == RunStatus.RUNNING.value,
)
Index(
    "ix_simulation_runs_dispatching",
    simulation_runs.c.updated_at,
    postgresql_where=simulation_runs.c.status == RunStatus.DISPATCHING.value,
)

_SIMULATION_RUN_KEY = [
    simulation_runs.c.id,
    simulation_runs.c.tax_benefit_model_version_id,
    simulation_runs.c.year,
]

report_runs = Table(
    "report_runs",
    simulation_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("report_id", Uuid(as_uuid=True), nullable=False),
    Column("report_kind", Text, nullable=False),
    Column("tax_benefit_model_version_id", Uuid(as_uuid=True), nullable=False),
    Column("year", Integer, nullable=False),
    Column("baseline_simulation_run_id", Uuid(as_uuid=True), nullable=False),
    Column("reform_simulation_run_id", Uuid(as_uuid=True), nullable=True),
    Column("trigger", Text, nullable=False),
    *_execution_columns("comparison_", required=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    # Set with an economy comparison's result rows (item 1.07): the optional
    # sections the result holds, its SPM provenance, if any, and the model and
    # data versions it reports (a US state run reports the revision of the
    # dataset it loaded, not the release's default data version).
    Column("result_sections", ARRAY(Text), nullable=True),
    Column("spm_provenance", JSONB(none_as_null=True), nullable=True),
    Column("result_model_version", Text, nullable=True),
    Column("result_data_version", Text, nullable=True),
    CheckConstraint(f"year BETWEEN {FIRST_YEAR} AND {LAST_YEAR}", name="year"),
    CheckConstraint(f"trigger IN ({_values(RunTrigger)})", name="trigger"),
    CheckConstraint(
        "spm_provenance IS NULL OR jsonb_typeof(spm_provenance) = 'object'",
        name="spm_provenance",
    ),
    # An economy report run compares two simulation runs in its own comparison
    # step; a household report run has no comparison step.
    CheckConstraint(
        "(report_kind = 'economy' AND reform_simulation_run_id IS NOT NULL "
        "AND comparison_status IS NOT NULL) "
        "OR (report_kind = 'household' AND comparison_status IS NULL "
        "AND comparison_modal_call_id IS NULL "
        "AND comparison_error_code IS NULL "
        "AND comparison_dispatched_at IS NULL "
        "AND comparison_next_check_at IS NULL "
        "AND comparison_completed_at IS NULL "
        "AND comparison_check_count = 0)",
        name="comparison_matches_kind",
    ),
    *_status_checks("comparison_", optional=True),
    ForeignKeyConstraint(
        ["report_id", "report_kind"],
        [reports.c.id, reports.c.kind],
        name="fk_report_runs_report",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        [
            "baseline_simulation_run_id",
            "tax_benefit_model_version_id",
            "year",
        ],
        _SIMULATION_RUN_KEY,
        name="fk_report_runs_baseline_simulation_run",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["reform_simulation_run_id", "tax_benefit_model_version_id", "year"],
        _SIMULATION_RUN_KEY,
        name="fk_report_runs_reform_simulation_run",
        ondelete="RESTRICT",
    ),
)
# A report's status is its newest report run's.
Index(
    "ix_report_runs_report_id_created_at",
    report_runs.c.report_id,
    report_runs.c.created_at,
)
Index(
    "ix_report_runs_baseline_simulation_run_id",
    report_runs.c.baseline_simulation_run_id,
)
Index(
    "ix_report_runs_reform_simulation_run_id",
    report_runs.c.reform_simulation_run_id,
)
Index(
    "ix_report_runs_due_comparison_checks",
    report_runs.c.comparison_next_check_at,
    postgresql_where=report_runs.c.comparison_status == RunStatus.RUNNING.value,
)
# Each recording pass looks for pending comparison steps whose run failed.
Index(
    "ix_report_runs_pending_comparisons",
    report_runs.c.id,
    postgresql_where=report_runs.c.comparison_status == RunStatus.PENDING.value,
)
Index(
    "ix_report_runs_comparisons_dispatching",
    report_runs.c.updated_at,
    postgresql_where=report_runs.c.comparison_status == RunStatus.DISPATCHING.value,
)


def bounded_error_message(message: str) -> str:
    """Return ``message`` cut to the length the run tables store."""
    return message[:MAX_ERROR_MESSAGE_LENGTH]


@dataclass(frozen=True)
class NewSimulationRun:
    """A simulation run to find or create.

    An economy run names the region and dataset rows its caller resolved in
    the run's catalog version: the simulation's region code, and the dataset
    the simulation names or, when it names none, the region's default dataset
    in that version. The database checks that both belong to that version. A
    household run names neither.
    """

    simulation_id: UUID
    tax_benefit_model_version_id: UUID
    year: int
    trigger: RunTrigger
    region_code: str | None = None
    dataset_id: UUID | None = None

    def __post_init__(self) -> None:
        """Reject a year outside the supported range without touching the database."""
        if not FIRST_YEAR <= self.year <= LAST_YEAR:
            msg = f"year must be between {FIRST_YEAR} and {LAST_YEAR}"
            raise ValueError(msg)


@dataclass(frozen=True)
class NewReportRun:
    """A report run to create from two simulation runs that already exist.

    The baseline run must be a run of the report's baseline simulation, and
    the reform run one of its reform simulation; an economy report run needs
    both. They must share a catalog version and year, which the report run
    takes from them.
    """

    report_id: UUID
    baseline_simulation_run_id: UUID
    trigger: RunTrigger
    reform_simulation_run_id: UUID | None = None


def _record_values(cls: type, row: RowMapping) -> dict[str, Any]:
    return {field.name: row[field.name] for field in fields(cls)}


@dataclass(frozen=True)
class SimulationRunRecord:
    """One ``simulation.simulation_runs`` row."""

    id: UUID
    simulation_id: UUID
    simulation_kind: SimulationKind
    tax_benefit_model_version_id: UUID
    year: int
    attempt: int
    region_code: str | None
    dataset_id: UUID | None
    trigger: RunTrigger
    status: RunStatus
    modal_call_id: str | None
    error_code: str | None
    error_message: str | None
    check_count: int
    dispatched_at: datetime | None
    next_check_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def finished(self) -> bool:
        """Whether the run has succeeded or failed."""
        return self.status in FINISHED_STATUSES

    @classmethod
    def from_row(cls, row: RowMapping) -> Self:
        """Build the record from a selected ``simulation_runs`` row."""
        values = _record_values(cls, row)
        values["simulation_kind"] = SimulationKind(values["simulation_kind"])
        values["trigger"] = RunTrigger(values["trigger"])
        values["status"] = RunStatus(values["status"])
        return cls(**values)


@dataclass(frozen=True)
class ReportRunRecord:
    """One ``simulation.report_runs`` row.

    The comparison fields are None for a household report run, which has no
    comparison step.
    """

    id: UUID
    report_id: UUID
    report_kind: ReportKind
    tax_benefit_model_version_id: UUID
    year: int
    baseline_simulation_run_id: UUID
    reform_simulation_run_id: UUID | None
    trigger: RunTrigger
    comparison_status: RunStatus | None
    comparison_modal_call_id: str | None
    comparison_error_code: str | None
    comparison_error_message: str | None
    comparison_check_count: int
    comparison_dispatched_at: datetime | None
    comparison_next_check_at: datetime | None
    comparison_completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: RowMapping) -> Self:
        """Build the record from a selected ``report_runs`` row."""
        values = _record_values(cls, row)
        values["report_kind"] = ReportKind(values["report_kind"])
        values["trigger"] = RunTrigger(values["trigger"])
        if values["comparison_status"] is not None:
            values["comparison_status"] = RunStatus(values["comparison_status"])
        return cls(**values)


@dataclass(frozen=True)
class StartedSimulationRun:
    """The simulation run a start resolved to, and whether this call created it."""

    run: SimulationRunRecord
    created: bool


@dataclass(frozen=True)
class StartedReportRun:
    """The report run a first start resolved to, and whether this call created it."""

    run: ReportRunRecord
    created: bool
