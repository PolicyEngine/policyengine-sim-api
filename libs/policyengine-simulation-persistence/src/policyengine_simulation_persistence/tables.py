"""Models of the Public API's tables the simulation service writes.

The Public API's v2 migration chain creates and changes these tables; this
module only describes them, so the service can write typed rows. Each table
matches what the chain builds, column for column, with its constraints and
indexes under the same names, its foreign keys included.

Some keys name rows of the Public API's own tables, which the Public API
writes: users, policies, dynamics, households, datasets, regions and the model
catalog. PostgreSQL checks each of those keys when a row is inserted, as the
referenced table's owner, so the service's role needs no privilege for it,
and a row naming one that doesn't exist fails with SQLSTATE 23503. Those
tables are modelled in ``referenced_metadata`` by their ``id`` alone, the
column every key names, so SQLAlchemy can declare the keys.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    Float,
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
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

VERSION_LENGTH: Final = 128
# A report's budget window covers at most this many years.
MAX_BUDGET_WINDOW_YEARS: Final = 75
LATEST_YEAR: Final = 2200
MODAL_CALL_ID_LENGTH: Final = 255
OUTPUT_URI_LENGTH: Final = 2048
SHA256_LENGTH: Final = 64
# A definition key is a SHA-256 in lowercase hexadecimal.
DEFINITION_KEY_LENGTH: Final = 64


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
    """Lifecycle state of a report run or a budget window's calculation."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReportRunTrigger(StrEnum):
    """What started a report run or a budget window's calculation."""

    INITIAL = "initial"
    MANUAL = "manual"
    SYSTEM = "system"


class HouseholdJobStatus(StrEnum):
    """Lifecycle state of a household job."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _enum_type(values: type[StrEnum], name: str) -> Enum:
    """Store each member's value, under the Postgres type the chain names.

    The type names its schema itself: SQLAlchemy 2.0 doesn't give an enum the
    metadata's schema, so it would otherwise follow the role's search_path.
    """
    return Enum(
        values,
        name=name,
        schema=TABLE_SCHEMA,
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


def _key_columns(*names: str) -> tuple[Column[int] | Column[str], ...]:
    """Return ``key_version`` and each named key column, nullable.

    A row created before the keys existed has none of them, and is never
    found by key.
    """
    return (
        Column("key_version", Integer, nullable=True),
        *(Column(name, String(DEFINITION_KEY_LENGTH), nullable=True) for name in names),
    )


def _key_checks(table_name: str, *names: str) -> tuple[CheckConstraint, ...]:
    """Require a positive key version, and each key in lowercase hexadecimal.

    The checks are the Public API's, under its names; ``names`` are the key
    columns.
    """
    return (
        CheckConstraint("key_version > 0", name=f"ck_{table_name}_key_version"),
        *(
            CheckConstraint(
                f"length({name}) = {DEFINITION_KEY_LENGTH} AND "
                f"ltrim({name}, '0123456789abcdef') = ''",
                name=f"ck_{table_name}_{name}",
            )
            for name in names
        ),
    )


def _forced_column() -> Column[bool]:
    """Return the marker of a forced re-run's run, which nothing joins."""
    return Column("forced", Boolean, nullable=False, server_default=false())


def _year_between(table_name: str, *, nullable: bool) -> CheckConstraint:
    condition = f"year BETWEEN 1900 AND {LATEST_YEAR}"
    if nullable:
        condition = f"year IS NULL OR {condition}"
    return CheckConstraint(condition, name=f"ck_{table_name}_year")


# Named explicitly, so writes reach the Public API's tables whatever the
# role's search_path.
TABLE_SCHEMA: Final = "public"
service_metadata = MetaData(schema=TABLE_SCHEMA)
# The Public API's own tables that the service's rows name, each by its id
# alone. They aren't models of those tables: nothing reads or writes them here.
referenced_metadata = MetaData(schema=TABLE_SCHEMA)
_PUBLIC_API_TABLES: Final = {
    table_name: Table(
        table_name,
        referenced_metadata,
        Column("id", Uuid, nullable=False),
        PrimaryKeyConstraint("id", name=f"pk_{table_name}"),
    )
    for table_name in (
        "datasets",
        "dynamics",
        "households",
        "policies",
        "regions",
        "tax_benefit_model_versions",
        "tax_benefit_models",
        "users",
    )
}


def _public_api_key(
    column_name: str, table_name: str, *, ondelete: str, name: str
) -> ForeignKeyConstraint:
    """Declare that ``column_name`` names a row of the Public API's ``table_name``.

    ``name`` is the key's name in the Public API's chain, which shortens a
    name past PostgreSQL's 63 characters and ends it in four characters of
    its hash.
    """
    return ForeignKeyConstraint(
        [column_name],
        [_PUBLIC_API_TABLES[table_name].c.id],
        name=name,
        ondelete=ondelete,
    )


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
    *_key_columns("definition_key"),
    PrimaryKeyConstraint("id", name="pk_simulations"),
    _public_api_key(
        "dataset_id",
        "datasets",
        ondelete="RESTRICT",
        name="fk_simulations_dataset_id_datasets",
    ),
    _public_api_key(
        "household_id",
        "households",
        ondelete="RESTRICT",
        name="fk_simulations_household_id_households",
    ),
    _public_api_key(
        "policy_id",
        "policies",
        ondelete="SET NULL",
        name="fk_simulations_policy_id_policies",
    ),
    _public_api_key(
        "dynamic_id",
        "dynamics",
        ondelete="SET NULL",
        name="fk_simulations_dynamic_id_dynamics",
    ),
    _public_api_key(
        "tax_benefit_model_version_id",
        "tax_benefit_model_versions",
        ondelete="RESTRICT",
        name="fk_simulations_tax_benefit_model_version_id_tax_benefit_84f8",
    ),
    _public_api_key(
        "region_id",
        "regions",
        ondelete="SET NULL",
        name="fk_simulations_region_id_regions",
    ),
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
    *_key_checks("simulations", "definition_key"),
    CheckConstraint(
        "(key_version IS NULL) = (definition_key IS NULL)",
        name="ck_simulations_keys_complete",
    ),
    # Find-or-create's conflict target: one simulation per definition.
    UniqueConstraint(
        "key_version",
        "definition_key",
        name="uq_simulations_key_version_definition_key",
    ),
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
    # The number of years from ``year`` an economy report's window covers.
    Column("budget_window_size", Integer, nullable=True),
    *_key_columns("definition_key", "dataset_independent_key"),
    PrimaryKeyConstraint("id", name="pk_reports"),
    _public_api_key(
        "user_id", "users", ondelete="SET NULL", name="fk_reports_user_id_users"
    ),
    _public_api_key(
        "tax_benefit_model_id",
        "tax_benefit_models",
        ondelete="RESTRICT",
        name="fk_reports_tax_benefit_model_id_tax_benefit_models",
    ),
    _public_api_key(
        "policy_id",
        "policies",
        ondelete="SET NULL",
        name="fk_reports_policy_id_policies",
    ),
    _public_api_key(
        "household_id",
        "households",
        ondelete="RESTRICT",
        name="fk_reports_household_id_households",
    ),
    _public_api_key(
        "dataset_id",
        "datasets",
        ondelete="SET NULL",
        name="fk_reports_dataset_id_datasets",
    ),
    _public_api_key(
        "region_id",
        "regions",
        ondelete="SET NULL",
        name="fk_reports_region_id_regions",
    ),
    ForeignKeyConstraint(
        ["baseline_simulation_id"],
        [f"{TABLE_SCHEMA}.simulations.id"],
        name="fk_reports_baseline_simulation_id_simulations",
        ondelete="SET NULL",
    ),
    ForeignKeyConstraint(
        ["reform_simulation_id"],
        [f"{TABLE_SCHEMA}.simulations.id"],
        name="fk_reports_reform_simulation_id_simulations",
        ondelete="SET NULL",
    ),
    CheckConstraint(
        "budget_window_size IS NULL OR (household_id IS NULL AND "
        f"budget_window_size BETWEEN 1 AND {MAX_BUDGET_WINDOW_YEARS} AND "
        f"year IS NOT NULL AND year + budget_window_size - 1 <= {LATEST_YEAR})",
        name="ck_reports_budget_window_size",
    ),
    _year_between("reports", nullable=True),
    *_key_checks("reports", "definition_key", "dataset_independent_key"),
    CheckConstraint(
        "(key_version IS NULL) = (definition_key IS NULL) AND "
        "(key_version IS NULL) = (dataset_independent_key IS NULL)",
        name="ck_reports_keys_complete",
    ),
    # Find-or-create's conflict target: one report per definition.
    UniqueConstraint(
        "key_version",
        "definition_key",
        name="uq_reports_key_version_definition_key",
    ),
    # Reports that differ only in their dataset share this key, by which
    # the app finds one from the other.
    Index("ix_reports_dataset_independent_key", "dataset_independent_key"),
    Index("ix_reports_country_type_created_at", "country", "type", "created_at"),
    Index("ix_reports_tax_benefit_model_id", "tax_benefit_model_id"),
    # Deleting a user sets its reports' user_id to null.
    Index("ix_reports_user_id", "user_id"),
)

simulation_runs = Table(
    "simulation_runs",
    service_metadata,
    _identifier(),
    _timestamp("created_at", server_now=True),
    _timestamp("updated_at", server_now=True),
    Column("simulation_id", Uuid, nullable=False),
    # The year it simulates; a budget window runs a simulation once per year.
    Column("year", Integer, nullable=False),
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
    _forced_column(),
    PrimaryKeyConstraint("id", name="pk_simulation_runs"),
    _public_api_key(
        "tax_benefit_model_version_id",
        "tax_benefit_model_versions",
        ondelete="RESTRICT",
        name="fk_simulation_runs_tax_benefit_model_version_id_tax_ben_1a94",
    ),
    _public_api_key(
        "dataset_id",
        "datasets",
        ondelete="RESTRICT",
        name="fk_simulation_runs_dataset_id_datasets",
    ),
    _public_api_key(
        "region_id",
        "regions",
        ondelete="SET NULL",
        name="fk_simulation_runs_region_id_regions",
    ),
    ForeignKeyConstraint(
        ["simulation_id"],
        [f"{TABLE_SCHEMA}.simulations.id"],
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
    _year_between("simulation_runs", nullable=False),
    Index(
        "ix_simulation_runs_current_output",
        "simulation_id",
        "year",
        "status",
        "country_package_version",
        "policyengine_version",
        "completed_at",
        "id",
    ),
    Index("ix_simulation_runs_status_started_at", "status", "started_at"),
    # One run that hasn't failed per simulation, year, catalog version and
    # versions, apart from forced re-runs' runs: find-or-create's conflict
    # target, so identical requests join one run.
    Index(
        "uq_simulation_runs_unfailed_unforced_versions",
        "simulation_id",
        "year",
        "tax_benefit_model_version_id",
        "country_package_version",
        "policyengine_version",
        unique=True,
        postgresql_where=text("status <> 'failed' AND NOT forced"),
    ),
    Index(
        "ix_simulation_runs_tax_benefit_model_version_id",
        "tax_benefit_model_version_id",
    ),
)

# One calculation of a report's budget window: a report run for each year.
budget_window_runs = Table(
    "budget_window_runs",
    service_metadata,
    _identifier(),
    _timestamp("created_at", server_now=True),
    _timestamp("updated_at", server_now=True),
    Column("report_id", Uuid, nullable=False),
    Column("tax_benefit_model_version_id", Uuid, nullable=False),
    Column(
        "trigger",
        _enum_type(ReportRunTrigger, "v2_report_run_trigger"),
        nullable=False,
    ),
    Column("recalculate", Boolean, nullable=False),
    Column(
        "status",
        _enum_type(ReportRunStatus, "v2_report_run_status"),
        nullable=False,
    ),
    Column("error_message", String, nullable=True),
    _timestamp("started_at"),
    _timestamp("completed_at"),
    PrimaryKeyConstraint("id", name="pk_budget_window_runs"),
    _public_api_key(
        "tax_benefit_model_version_id",
        "tax_benefit_model_versions",
        ondelete="RESTRICT",
        name="fk_budget_window_runs_tax_benefit_model_version_id_tax__5106",
    ),
    ForeignKeyConstraint(
        ["report_id"],
        [f"{TABLE_SCHEMA}.reports.id"],
        name="fk_budget_window_runs_report_id_reports",
        ondelete="CASCADE",
    ),
    # The target of report_runs' key, which keeps a year run to its window
    # run's report.
    UniqueConstraint("id", "report_id", name="uq_budget_window_runs_id_report"),
    _terminal_completion("budget_window_runs"),
    Index(
        "ix_budget_window_runs_report_id_created_at",
        "report_id",
        "created_at",
    ),
    Index(
        "ix_budget_window_runs_tax_benefit_model_version_id",
        "tax_benefit_model_version_id",
    ),
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
    # The pair it compares; a household report with only a baseline has no
    # reform run.
    Column("baseline_simulation_run_id", Uuid, nullable=False),
    Column("reform_simulation_run_id", Uuid, nullable=True),
    # Set on each year's run of a budget window, with that year.
    Column("budget_window_run_id", Uuid, nullable=True),
    Column("year", Integer, nullable=True),
    Column("modal_call_id", String(MODAL_CALL_ID_LENGTH), nullable=True),
    # Cliff impact: all four values or none.
    Column("baseline_cliff_gap", Float, nullable=True),
    Column("baseline_cliff_share", Float, nullable=True),
    Column("reform_cliff_gap", Float, nullable=True),
    Column("reform_cliff_share", Float, nullable=True),
    Column("spm_provenance", JSONB, nullable=True),
    Column("data_version", String(VERSION_LENGTH), nullable=True),
    _forced_column(),
    PrimaryKeyConstraint("id", name="pk_report_runs"),
    ForeignKeyConstraint(
        ["report_id"],
        [f"{TABLE_SCHEMA}.reports.id"],
        name="fk_report_runs_report_id_reports",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["baseline_simulation_run_id"],
        [f"{TABLE_SCHEMA}.simulation_runs.id"],
        name="fk_report_runs_baseline_simulation_run_id_simulation_runs",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["reform_simulation_run_id"],
        [f"{TABLE_SCHEMA}.simulation_runs.id"],
        name="fk_report_runs_reform_simulation_run_id_simulation_runs",
        ondelete="RESTRICT",
    ),
    # The window run belongs to the same report, and deleting it deletes its
    # year runs.
    ForeignKeyConstraint(
        ["budget_window_run_id", "report_id"],
        [
            f"{TABLE_SCHEMA}.budget_window_runs.id",
            f"{TABLE_SCHEMA}.budget_window_runs.report_id",
        ],
        name="fk_report_runs_budget_window_run_id_budget_window_runs",
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "report_id",
        "idempotency_key",
        name="uq_report_runs_report_idempotency_key",
    ),
    UniqueConstraint(
        "budget_window_run_id",
        "year",
        name="uq_report_runs_budget_window_run_id_year",
    ),
    CheckConstraint(
        "budget_window_run_id IS NULL OR year IS NOT NULL",
        name="ck_report_runs_budget_window_year",
    ),
    CheckConstraint(
        "(baseline_cliff_gap IS NULL) = (baseline_cliff_share IS NULL) AND "
        "(baseline_cliff_gap IS NULL) = (reform_cliff_gap IS NULL) AND "
        "(baseline_cliff_gap IS NULL) = (reform_cliff_share IS NULL)",
        name="ck_report_runs_cliff_impact_complete",
    ),
    _terminal_completion("report_runs"),
    _year_between("report_runs", nullable=True),
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
    # One run that hasn't failed per report and versions, apart from forced
    # re-runs' runs and a budget window's years: find-or-create's conflict
    # target.
    Index(
        "uq_report_runs_unfailed_unforced_versions",
        "report_id",
        "country_package_version",
        "policyengine_version",
        unique=True,
        postgresql_where=text(
            "budget_window_run_id IS NULL AND status <> 'failed' AND NOT forced"
        ),
    ),
    Index(
        "ix_report_runs_baseline_simulation_run_id",
        "baseline_simulation_run_id",
    ),
    Index("ix_report_runs_reform_simulation_run_id", "reform_simulation_run_id"),
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
    _public_api_key(
        "household_id",
        "households",
        ondelete="RESTRICT",
        name="fk_household_jobs_household_id_households",
    ),
    _public_api_key(
        "policy_id",
        "policies",
        ondelete="SET NULL",
        name="fk_household_jobs_policy_id_policies",
    ),
    _public_api_key(
        "dynamic_id",
        "dynamics",
        ondelete="SET NULL",
        name="fk_household_jobs_dynamic_id_dynamics",
    ),
    _public_api_key(
        "tax_benefit_model_version_id",
        "tax_benefit_model_versions",
        ondelete="RESTRICT",
        name="fk_household_jobs_tax_benefit_model_version_id_tax_bene_82b2",
    ),
    _terminal_completion("household_jobs"),
    _year_between("household_jobs", nullable=False),
    Index("ix_household_jobs_household_id", "household_id"),
    Index("ix_household_jobs_status_created_at", "status", "created_at"),
    Index(
        "ix_household_jobs_tax_benefit_model_version_id",
        "tax_benefit_model_version_id",
    ),
)
