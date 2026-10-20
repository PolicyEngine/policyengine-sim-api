"""Report tables: ``simulation.reports`` and ``simulation.report_results``.

A report is an economy or household comparison a user can save. A report row
created by the one-time migration from the Public API keeps its v1 report ID in
``legacy_report_id`` and starts ``not_started``, with no job. Once a job is
attached the report is ``submitted``, and its progress is that job's status,
so the two never disagree.

``report_results`` holds one validated aggregate per finished job, so reports
with identical inputs (and therefore one shared job) share one stored result.
The aggregate is JSONB validated by the Pydantic model registered for its
report kind and schema version, stored with its SHA-256, summary columns for
queries, and references to any stored artifacts.

Nothing writes these tables yet: Step 2 of the API v2 migration adds the
report routes and registers the report job kinds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID, uuid4

from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    Uuid,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from policyengine_simulation_persistence.canonical import result_sha256, utc_now
from policyengine_simulation_persistence.schema import (
    SIMULATION_SCHEMA,
    simulation_metadata,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from sqlalchemy import Engine, RowMapping, Select

COUNTRY_PATTERN = re.compile(r"[a-z]{2}")


class ReportKind(StrEnum):
    """What a report compares."""

    ECONOMY = "economy"
    HOUSEHOLD = "household"


class ReportStatus(StrEnum):
    """Whether a report has a job; a submitted report's progress is its job's."""

    NOT_STARTED = "not_started"
    SUBMITTED = "submitted"


class UnsupportedReportAggregateError(ValueError):
    """No aggregate model is registered for this report kind and version."""


# The model each report kind's aggregate is validated against, by schema
# version. Household aggregates are registered when their contract exists.
REPORT_AGGREGATE_MODELS: dict[tuple[ReportKind, int], type[BaseModel]] = {
    (ReportKind.ECONOMY, 1): SingleYearMacroOutput,
}


def validate_report_aggregate(
    kind: ReportKind,
    schema_version: int,
    aggregate: object,
) -> BaseModel:
    """Return ``aggregate`` validated by its registered model.

    Raise UnsupportedReportAggregateError when no model is registered, and
    pydantic.ValidationError when the aggregate does not match it.
    """
    model = REPORT_AGGREGATE_MODELS.get((kind, schema_version))
    if model is None:
        msg = f"no aggregate model for {kind.value} reports, version {schema_version}"
        raise UnsupportedReportAggregateError(msg)
    return model.model_validate(aggregate)


def report_summary(kind: ReportKind, aggregate: BaseModel) -> dict[str, Any]:
    """Return the summary columns a validated aggregate fills."""
    if kind is ReportKind.ECONOMY and isinstance(aggregate, SingleYearMacroOutput):
        budget = aggregate.budget
        return {
            "budgetary_impact": budget.budgetary_impact,
            "tax_revenue_impact": budget.tax_revenue_impact,
            "state_tax_revenue_impact": budget.state_tax_revenue_impact,
            "benefit_spending_impact": budget.benefit_spending_impact,
            "households": budget.households,
            "baseline_net_income": budget.baseline_net_income,
            "model_version": aggregate.model_version,
            "data_version": aggregate.data_version,
        }
    return {}


class ReportArtifact(BaseModel):
    """A stored file a report result was computed from or exported to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
    uri: Annotated[str, Field(pattern=r"^(gs|hf|https)://\S+$", max_length=2048)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    media_type: Annotated[str, Field(min_length=1, max_length=255)]


_artifacts_adapter = TypeAdapter(tuple[ReportArtifact, ...])
# jobs.py defines simulation.jobs and imports this module, so the foreign keys
# name it rather than import it.
JOBS_ID = f"{SIMULATION_SCHEMA}.jobs.id"
_KIND_VALUES = ", ".join(f"'{kind.value}'" for kind in ReportKind)
_SHA256_PATTERN = "^[0-9a-f]{64}$"
SUMMARY_COLUMNS = (
    "budgetary_impact",
    "tax_revenue_impact",
    "state_tax_revenue_impact",
    "benefit_spending_impact",
    "households",
    "baseline_net_income",
    "model_version",
    "data_version",
)

reports = Table(
    "reports",
    simulation_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("country", Text, nullable=False),
    Column("legacy_report_id", BigInteger, nullable=True, unique=True),
    Column("job_id", Uuid(as_uuid=True), ForeignKey(JOBS_ID), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(f"kind IN ({_KIND_VALUES})", name="kind"),
    CheckConstraint("country ~ '^[a-z]{2}$'", name="country"),
    CheckConstraint(
        "(status = 'not_started' AND job_id IS NULL) "
        "OR (status = 'submitted' AND job_id IS NOT NULL)",
        name="status_matches_job",
    ),
)
Index("ix_reports_job_id", reports.c.job_id)

report_results = Table(
    "report_results",
    simulation_metadata,
    Column("job_id", Uuid(as_uuid=True), ForeignKey(JOBS_ID), primary_key=True),
    Column("report_kind", Text, nullable=False),
    Column("aggregate", JSONB, nullable=False),
    Column("aggregate_schema_version", Integer, nullable=False),
    Column("aggregate_sha256", String(64), nullable=False),
    Column("budgetary_impact", Float, nullable=True),
    Column("tax_revenue_impact", Float, nullable=True),
    Column("state_tax_revenue_impact", Float, nullable=True),
    Column("benefit_spending_impact", Float, nullable=True),
    Column("households", Float, nullable=True),
    Column("baseline_net_income", Float, nullable=True),
    Column("model_version", Text, nullable=True),
    Column("data_version", Text, nullable=True),
    Column("artifacts", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(f"report_kind IN ({_KIND_VALUES})", name="report_kind"),
    CheckConstraint(f"aggregate_sha256 ~ '{_SHA256_PATTERN}'", name="aggregate_sha256"),
    CheckConstraint("aggregate_schema_version >= 1", name="aggregate_schema_version"),
    CheckConstraint("jsonb_typeof(artifacts) = 'array'", name="artifacts"),
)


def report_result_values(
    *,
    report_kind: ReportKind,
    aggregate: object,
    aggregate_schema_version: int,
    artifacts: tuple[ReportArtifact, ...],
) -> dict[str, Any]:
    """Validate an aggregate and return the ``report_results`` columns it fills.

    The job ID and creation time are added by the writer.
    """
    validated = validate_report_aggregate(
        report_kind, aggregate_schema_version, aggregate
    )
    stored_aggregate = validated.model_dump(mode="json")
    return {
        "report_kind": report_kind.value,
        "aggregate": stored_aggregate,
        "aggregate_schema_version": aggregate_schema_version,
        "aggregate_sha256": result_sha256(stored_aggregate),
        **dict.fromkeys(SUMMARY_COLUMNS),
        **report_summary(report_kind, validated),
        "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
    }


@dataclass(frozen=True)
class NewReport:
    """A report to create; with a job it is submitted, without one not started."""

    kind: ReportKind
    country: str
    job_id: UUID | None = None
    legacy_report_id: int | None = None

    def __post_init__(self) -> None:
        """Reject a country that is not a lowercase two-letter code."""
        if not COUNTRY_PATTERN.fullmatch(self.country):
            msg = "country must be a lowercase two-letter code"
            raise ValueError(msg)


@dataclass(frozen=True)
class ReportRecord:
    """One ``simulation.reports`` row."""

    id: UUID
    kind: ReportKind
    status: ReportStatus
    country: str
    legacy_report_id: int | None
    job_id: UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: RowMapping) -> ReportRecord:
        """Build the record from a selected ``reports`` row."""
        values = {field.name: row[field.name] for field in fields(cls)}
        values["kind"] = ReportKind(values["kind"])
        values["status"] = ReportStatus(values["status"])
        return cls(**values)


@dataclass(frozen=True)
class ReportResultRecord:
    """One ``simulation.report_results`` row."""

    job_id: UUID
    report_kind: ReportKind
    aggregate: dict[str, Any]
    aggregate_schema_version: int
    aggregate_sha256: str
    budgetary_impact: float | None
    tax_revenue_impact: float | None
    state_tax_revenue_impact: float | None
    benefit_spending_impact: float | None
    households: float | None
    baseline_net_income: float | None
    model_version: str | None
    data_version: str | None
    artifacts: tuple[ReportArtifact, ...]
    created_at: datetime

    @classmethod
    def from_row(cls, row: RowMapping) -> ReportResultRecord:
        """Build the record from a selected ``report_results`` row."""
        values = {field.name: row[field.name] for field in fields(cls)}
        values["report_kind"] = ReportKind(values["report_kind"])
        values["artifacts"] = _artifacts_adapter.validate_python(values["artifacts"])
        return cls(**values)


class ReportStore:
    """Typed SQLAlchemy Core reads and writes for report rows."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Use ``engine`` (the runtime role) and ``clock`` for every timestamp."""
        self.engine = engine
        self._clock = clock

    def _values(self, new_report: NewReport) -> dict[str, Any]:
        now = self._clock()
        return {
            "id": uuid4(),
            "kind": new_report.kind.value,
            "status": (
                ReportStatus.NOT_STARTED.value
                if new_report.job_id is None
                else ReportStatus.SUBMITTED.value
            ),
            "country": new_report.country,
            "legacy_report_id": new_report.legacy_report_id,
            "job_id": new_report.job_id,
            "created_at": now,
            "updated_at": now,
        }

    def create_report(self, new_report: NewReport) -> ReportRecord:
        """Insert one report and return it."""
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    postgres_insert(reports)
                    .values(**self._values(new_report))
                    .returning(*reports.c)
                )
                .mappings()
                .one()
            )
        return ReportRecord.from_row(row)

    def create_migrated_report(
        self,
        *,
        legacy_report_id: int,
        kind: ReportKind,
        country: str,
    ) -> ReportRecord:
        """Create a not-started report for a v1 report, or return the existing one."""
        values = self._values(
            NewReport(kind=kind, country=country, legacy_report_id=legacy_report_id)
        )
        with self.engine.begin() as connection:
            created = (
                connection.execute(
                    postgres_insert(reports)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=[reports.c.legacy_report_id])
                    .returning(*reports.c)
                )
                .mappings()
                .first()
            )
            row = (
                created
                or connection.execute(
                    select(reports).where(
                        reports.c.legacy_report_id == legacy_report_id
                    )
                )
                .mappings()
                .one()
            )
        return ReportRecord.from_row(row)

    def get(self, report_id: UUID) -> ReportRecord | None:
        """Return the report with this ID, or None."""
        return self._one(select(reports).where(reports.c.id == report_id))

    def get_by_legacy_report_id(self, legacy_report_id: int) -> ReportRecord | None:
        """Return the report migrated from this v1 report, or None."""
        return self._one(
            select(reports).where(reports.c.legacy_report_id == legacy_report_id)
        )

    def _one(self, statement: Select[Any]) -> ReportRecord | None:
        with self.engine.connect() as connection:
            row = connection.execute(statement).mappings().first()
        return None if row is None else ReportRecord.from_row(row)

    def get_result(self, job_id: UUID) -> ReportResultRecord | None:
        """Return the stored report result of a finished job, or None."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(report_results).where(report_results.c.job_id == job_id)
                )
                .mappings()
                .first()
            )
        return None if row is None else ReportResultRecord.from_row(row)
