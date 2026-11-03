"""Models of the Public API's tables the simulation service writes.

The Public API's v2 migration chain creates and changes these tables; this
module only describes them, so the service can write typed rows. Each table
matches what the chain builds, column for column, with its constraints and
indexes under the same names. Foreign keys stay between these tables: the
policy, household and catalog rows they name are written by another service,
so the simulation service checks them itself.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

VERSION_LENGTH: Final = 128
MODAL_CALL_ID_LENGTH: Final = 255
OUTPUT_URI_LENGTH: Final = 2048
SHA256_LENGTH: Final = 64


class SimulationType(StrEnum):
    """Whether a simulation runs on one household or on a population dataset."""

    HOUSEHOLD = "household"
    ECONOMY = "economy"


class SimulationStatus(StrEnum):
    """Lifecycle state of a simulation run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReportRunStatus(StrEnum):
    """Lifecycle state of a report run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReportRunTrigger(StrEnum):
    """What started a report run."""

    INITIAL = "initial"
    MANUAL = "manual"
    SYSTEM = "system"


class SimulationRole(StrEnum):
    """The part a simulation run plays in a report run."""

    BASELINE = "baseline"
    REFORM = "reform"


class HouseholdJobStatus(StrEnum):
    """Lifecycle state of a household job."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _enum_type(values: type[StrEnum], name: str) -> Enum:
    """Store each member's value, under the Postgres type the chain names."""
    return Enum(
        values,
        name=name,
        values_callable=lambda members: [member.value for member in members],
    )


def _identifier() -> Column[UUID]:
    return Column("id", Uuid, nullable=False, default=uuid4)


def _timestamp(name: str, *, server_now: bool = False) -> Column[datetime]:
    """Return a timezone-aware timestamp column, defaulting to now() if asked."""
    if server_now:
        return Column(
            name,
            DateTime(timezone=True),
            nullable=False,
            server_default=text("now()"),
        )
    return Column(name, DateTime(timezone=True), nullable=True)


def _terminal_completion(table_name: str) -> CheckConstraint:
    """Require a succeeded or failed row to record when it completed."""
    return CheckConstraint(
        "status NOT IN ('succeeded', 'failed') OR completed_at IS NOT NULL",
        name=f"ck_{table_name}_terminal_completion",
    )


def _year_between(table_name: str, *, nullable: bool) -> CheckConstraint:
    condition = "year BETWEEN 1900 AND 2200"
    if nullable:
        condition = f"year IS NULL OR {condition}"
    return CheckConstraint(condition, name=f"ck_{table_name}_year")


service_metadata = MetaData()

simulations = Table(
    "simulations",
    service_metadata,
    _identifier(),
    _timestamp("created_at", server_now=True),
    _timestamp("updated_at", server_now=True),
    Column(
        "simulation_type",
        _enum_type(SimulationType, "v2_simulation_type"),
        nullable=False,
    ),
    Column("dataset_id", Uuid, nullable=True),
    Column("household_id", Uuid, nullable=True),
    Column("policy_id", Uuid, nullable=True),
    Column("dynamic_id", Uuid, nullable=True),
    Column("tax_benefit_model_version_id", Uuid, nullable=False),
    Column("region_id", Uuid, nullable=True),
    Column("filter_field", String(128), nullable=True),
    Column("filter_value", String(255), nullable=True),
    Column("filter_strategy", String(64), nullable=True),
    Column("year", Integer, nullable=True),
    PrimaryKeyConstraint("id", name="pk_simulations"),
    CheckConstraint(
        "(filter_field IS NULL) = (filter_value IS NULL)",
        name="ck_simulations_filter_pair",
    ),
    CheckConstraint(
        "(simulation_type = 'household' AND household_id IS NOT NULL "
        "AND dataset_id IS NULL) OR "
        "(simulation_type = 'economy' AND dataset_id IS NOT NULL "
        "AND household_id IS NULL)",
        name="ck_simulations_type_input",
    ),
    _year_between("simulations", nullable=True),
    Index(
        "ix_simulations_tax_benefit_model_version_id",
        "tax_benefit_model_version_id",
    ),
)

reports = Table(
    "reports",
    service_metadata,
    _identifier(),
    _timestamp("created_at", server_now=True),
    _timestamp("updated_at", server_now=True),
    Column("label", String(255), nullable=False),
    Column("description", String, nullable=True),
    Column("country", String(16), nullable=False),
    Column("type", String(128), nullable=True),
    Column("user_id", Uuid, nullable=True),
    Column("tax_benefit_model_id", Uuid, nullable=False),
    Column("policy_id", Uuid, nullable=True),
    Column("baseline_simulation_id", Uuid, nullable=True),
    Column("reform_simulation_id", Uuid, nullable=True),
    Column("household_id", Uuid, nullable=True),
    Column("dataset_id", Uuid, nullable=True),
    Column("region_id", Uuid, nullable=True),
    Column("year", Integer, nullable=True),
    Column("inputs", JSON, nullable=False),
    PrimaryKeyConstraint("id", name="pk_reports"),
    ForeignKeyConstraint(
        ["baseline_simulation_id"],
        ["simulations.id"],
        name="fk_reports_baseline_simulation_id_simulations",
        ondelete="SET NULL",
    ),
    ForeignKeyConstraint(
        ["reform_simulation_id"],
        ["simulations.id"],
        name="fk_reports_reform_simulation_id_simulations",
        ondelete="SET NULL",
    ),
    _year_between("reports", nullable=True),
    Index("ix_reports_country_type_created_at", "country", "type", "created_at"),
    Index("ix_reports_tax_benefit_model_id", "tax_benefit_model_id"),
)

simulation_runs = Table(
    "simulation_runs",
    service_metadata,
    _identifier(),
    _timestamp("created_at", server_now=True),
    _timestamp("updated_at", server_now=True),
    Column("simulation_id", Uuid, nullable=False),
    Column("tax_benefit_model_version_id", Uuid, nullable=False),
    Column("country_package_version", String(VERSION_LENGTH), nullable=False),
    Column("policyengine_version", String(VERSION_LENGTH), nullable=False),
    Column("dataset_id", Uuid, nullable=True),
    Column("region_id", Uuid, nullable=True),
    Column(
        "status",
        _enum_type(SimulationStatus, "v2_simulation_status"),
        nullable=False,
    ),
    Column("modal_call_id", String(MODAL_CALL_ID_LENGTH), nullable=True),
    Column("error_message", String, nullable=True),
    _timestamp("started_at"),
    _timestamp("completed_at"),
    Column("household_result", JSON, nullable=True),
    Column("output_uri", String(OUTPUT_URI_LENGTH), nullable=True),
    Column("output_sha256", String(SHA256_LENGTH), nullable=True),
    PrimaryKeyConstraint("id", name="pk_simulation_runs"),
    ForeignKeyConstraint(
        ["simulation_id"],
        ["simulations.id"],
        name="fk_simulation_runs_simulation_id_simulations",
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "(output_uri IS NULL) = (output_sha256 IS NULL)",
        name="ck_simulation_runs_output_pair",
    ),
    CheckConstraint(
        f"output_sha256 IS NULL OR length(output_sha256) = {SHA256_LENGTH}",
        name="ck_simulation_runs_output_sha256_length",
    ),
    _terminal_completion("simulation_runs"),
    Index(
        "ix_simulation_runs_current_output",
        "simulation_id",
        "status",
        "country_package_version",
        "policyengine_version",
        "completed_at",
        "id",
    ),
    Index("ix_simulation_runs_status_started_at", "status", "started_at"),
)

report_runs = Table(
    "report_runs",
    service_metadata,
    _identifier(),
    _timestamp("created_at", server_now=True),
    _timestamp("updated_at", server_now=True),
    Column("report_id", Uuid, nullable=False),
    Column("country_package_version", String(VERSION_LENGTH), nullable=False),
    Column("policyengine_version", String(VERSION_LENGTH), nullable=False),
    Column(
        "status",
        _enum_type(ReportRunStatus, "v2_report_run_status"),
        nullable=False,
    ),
    Column(
        "trigger",
        _enum_type(ReportRunTrigger, "v2_report_run_trigger"),
        nullable=False,
    ),
    Column("idempotency_key", Uuid, nullable=True),
    _timestamp("started_at"),
    _timestamp("completed_at"),
    Column("error_message", String, nullable=True),
    Column("markdown", Text, nullable=True),
    Column("modal_call_id", String(MODAL_CALL_ID_LENGTH), nullable=True),
    Column("labor_supply_result", JSON, nullable=True),
    Column("cliff_impact_result", JSON, nullable=True),
    Column("spm_provenance", JSON, nullable=True),
    Column("data_version", String(VERSION_LENGTH), nullable=True),
    PrimaryKeyConstraint("id", name="pk_report_runs"),
    ForeignKeyConstraint(
        ["report_id"],
        ["reports.id"],
        name="fk_report_runs_report_id_reports",
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "report_id",
        "idempotency_key",
        name="uq_report_runs_report_idempotency_key",
    ),
    _terminal_completion("report_runs"),
    Index(
        "ix_report_runs_current_output",
        "report_id",
        "status",
        "country_package_version",
        "policyengine_version",
        "completed_at",
        "id",
    ),
    Index("ix_report_runs_status_started_at", "status", "started_at"),
)

report_run_simulation_runs = Table(
    "report_run_simulation_runs",
    service_metadata,
    _identifier(),
    _timestamp("created_at", server_now=True),
    Column("report_run_id", Uuid, nullable=False),
    Column("simulation_run_id", Uuid, nullable=False),
    Column("role", _enum_type(SimulationRole, "v2_simulation_role"), nullable=False),
    PrimaryKeyConstraint("id", name="pk_report_run_simulation_runs"),
    ForeignKeyConstraint(
        ["report_run_id"],
        ["report_runs.id"],
        name="fk_report_run_simulation_runs_report_run_id_report_runs",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["simulation_run_id"],
        ["simulation_runs.id"],
        name="fk_report_run_simulation_runs_simulation_run_id_simulation_runs",
        ondelete="RESTRICT",
    ),
    UniqueConstraint(
        "report_run_id",
        "role",
        "simulation_run_id",
        name="uq_report_run_simulation_runs_member",
    ),
    Index(
        "ix_report_run_simulation_runs_simulation_run_id",
        "simulation_run_id",
    ),
)

household_jobs = Table(
    "household_jobs",
    service_metadata,
    _identifier(),
    _timestamp("created_at", server_now=True),
    Column("country", String(16), nullable=False),
    Column("request_data", JSON, nullable=False),
    Column("policy_id", Uuid, nullable=True),
    Column("dynamic_id", Uuid, nullable=True),
    Column(
        "status",
        _enum_type(HouseholdJobStatus, "v2_household_job_status"),
        nullable=False,
    ),
    Column("error_message", String, nullable=True),
    Column("result", JSON, nullable=True),
    _timestamp("started_at"),
    _timestamp("completed_at"),
    _timestamp("updated_at", server_now=True),
    Column("household_id", Uuid, nullable=False),
    Column("year", Integer, nullable=False),
    Column("tax_benefit_model_version_id", Uuid, nullable=False),
    Column("country_package_version", String(VERSION_LENGTH), nullable=False),
    Column("policyengine_version", String(VERSION_LENGTH), nullable=False),
    Column("modal_call_id", String(MODAL_CALL_ID_LENGTH), nullable=True),
    PrimaryKeyConstraint("id", name="pk_household_jobs"),
    _terminal_completion("household_jobs"),
    _year_between("household_jobs", nullable=False),
    Index("ix_household_jobs_household_id", "household_id"),
    Index("ix_household_jobs_status_created_at", "status", "created_at"),
)
