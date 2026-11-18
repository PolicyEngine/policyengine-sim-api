"""Models of API v1's tables the simulation service writes.

API v1's v2 migration chain creates and changes these tables; this module only
describes them, as typed mapped classes, so the service can write typed rows.
Each class matches what the chain builds, column for column, with its
constraints and indexes under the same names, its foreign keys included. Keys
to rows API v1 itself writes name the classes in ``referenced_tables``.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # SQLAlchemy reads Mapped annotations
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Final
from uuid import UUID  # noqa: TC003  # SQLAlchemy reads Mapped annotations at run time

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from policyengine_simulation_persistence.orm_base import (
    TABLE_SCHEMA,
    ServiceBase,
    enum_type,
    identifier_column,
    optional_timestamp_column,
    server_timestamp_column,
)
from policyengine_simulation_persistence.referenced_tables import (
    ReferencedDataset,
    ReferencedDynamic,
    ReferencedHousehold,
    ReferencedPolicy,
    ReferencedRegion,
    ReferencedTaxBenefitModel,
    ReferencedTaxBenefitModelVersion,
    ReferencedUser,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import InstrumentedAttribute, MappedColumn

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


def _completion_required(table_name: str) -> CheckConstraint:
    """Require a succeeded or failed row to record when it completed."""
    return CheckConstraint(
        "status NOT IN ('succeeded', 'failed') OR completed_at IS NOT NULL",
        name=f"ck_{table_name}_terminal_completion",
    )


def _key_version_column() -> MappedColumn[int | None]:
    """Return ``key_version``, empty on a row created before the keys existed.

    Such a row has none of the key columns, and is never found by key.
    """
    return mapped_column(Integer, nullable=True)


def _key_column() -> MappedColumn[str | None]:
    """Return a definition key column, empty on a row older than the keys."""
    return mapped_column(String(DEFINITION_KEY_LENGTH), nullable=True)


def _key_checks(table_name: str, *names: str) -> tuple[CheckConstraint, ...]:
    """Require a positive key version, and each key in lowercase hexadecimal.

    The checks are API v1's, under its names; ``names`` are the key columns.
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


def _forced_column() -> MappedColumn[bool]:
    """Return the marker of a forced re-run's run, which nothing joins."""
    return mapped_column(Boolean, nullable=False, server_default=false())


def _year_between(table_name: str, *, nullable: bool) -> CheckConstraint:
    condition = f"year BETWEEN 1900 AND {LATEST_YEAR}"
    if nullable:
        condition = f"year IS NULL OR {condition}"
    return CheckConstraint(condition, name=f"ck_{table_name}_year")


def _api_v1_key(
    column_name: str,
    referenced_id: InstrumentedAttribute[UUID],
    *,
    ondelete: str,
    name: str,
) -> ForeignKeyConstraint:
    """Declare that ``column_name`` names a row of one of API v1's own tables.

    ``name`` is the key's name in API v1's chain, which shortens a name past
    PostgreSQL's 63 characters and ends it in four characters of its hash.
    """
    return ForeignKeyConstraint(
        [column_name],
        [referenced_id],
        name=name,
        ondelete=ondelete,
    )


class Simulation(ServiceBase):
    """What one simulation calculates; its calculations are ``SimulationRun`` rows."""

    __tablename__ = "simulations"
    __table_args__: ClassVar = (
        PrimaryKeyConstraint("id", name="pk_simulations"),
        _api_v1_key(
            "dataset_id",
            ReferencedDataset.id,
            ondelete="RESTRICT",
            name="fk_simulations_dataset_id_datasets",
        ),
        _api_v1_key(
            "household_id",
            ReferencedHousehold.id,
            ondelete="RESTRICT",
            name="fk_simulations_household_id_households",
        ),
        _api_v1_key(
            "policy_id",
            ReferencedPolicy.id,
            ondelete="SET NULL",
            name="fk_simulations_policy_id_policies",
        ),
        _api_v1_key(
            "dynamic_id",
            ReferencedDynamic.id,
            ondelete="SET NULL",
            name="fk_simulations_dynamic_id_dynamics",
        ),
        _api_v1_key(
            "tax_benefit_model_version_id",
            ReferencedTaxBenefitModelVersion.id,
            ondelete="RESTRICT",
            name="fk_simulations_tax_benefit_model_version_id_tax_benefit_84f8",
        ),
        _api_v1_key(
            "region_id",
            ReferencedRegion.id,
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

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    updated_at: Mapped[datetime] = server_timestamp_column()
    simulation_type: Mapped[SimulationType] = mapped_column(
        enum_type(SimulationType, "v2_simulation_type")
    )
    dataset_id: Mapped[UUID | None] = mapped_column(Uuid)
    household_id: Mapped[UUID | None] = mapped_column(Uuid)
    policy_id: Mapped[UUID | None] = mapped_column(Uuid)
    dynamic_id: Mapped[UUID | None] = mapped_column(Uuid)
    tax_benefit_model_version_id: Mapped[UUID] = mapped_column(Uuid)
    region_id: Mapped[UUID | None] = mapped_column(Uuid)
    filter_field: Mapped[str | None] = mapped_column(String(128))
    filter_value: Mapped[str | None] = mapped_column(String(255))
    filter_strategy: Mapped[str | None] = mapped_column(String(64))
    year: Mapped[int | None] = mapped_column(Integer)
    key_version: Mapped[int | None] = _key_version_column()
    definition_key: Mapped[str | None] = _key_column()


class Report(ServiceBase):
    """What one report compares; its calculations are ``ReportRun`` rows."""

    __tablename__ = "reports"
    __table_args__: ClassVar = (
        PrimaryKeyConstraint("id", name="pk_reports"),
        _api_v1_key(
            "user_id",
            ReferencedUser.id,
            ondelete="SET NULL",
            name="fk_reports_user_id_users",
        ),
        _api_v1_key(
            "tax_benefit_model_id",
            ReferencedTaxBenefitModel.id,
            ondelete="RESTRICT",
            name="fk_reports_tax_benefit_model_id_tax_benefit_models",
        ),
        _api_v1_key(
            "policy_id",
            ReferencedPolicy.id,
            ondelete="SET NULL",
            name="fk_reports_policy_id_policies",
        ),
        _api_v1_key(
            "household_id",
            ReferencedHousehold.id,
            ondelete="RESTRICT",
            name="fk_reports_household_id_households",
        ),
        _api_v1_key(
            "dataset_id",
            ReferencedDataset.id,
            ondelete="SET NULL",
            name="fk_reports_dataset_id_datasets",
        ),
        _api_v1_key(
            "region_id",
            ReferencedRegion.id,
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

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    updated_at: Mapped[datetime] = server_timestamp_column()
    # Per-user columns the service no longer writes (C1 · 24's answer 6): a
    # report's name and description live on each user's saved report.
    label: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String)
    country: Mapped[str] = mapped_column(String(16))
    type: Mapped[str | None] = mapped_column(String(128))
    user_id: Mapped[UUID | None] = mapped_column(Uuid)
    tax_benefit_model_id: Mapped[UUID] = mapped_column(Uuid)
    policy_id: Mapped[UUID | None] = mapped_column(Uuid)
    baseline_simulation_id: Mapped[UUID | None] = mapped_column(Uuid)
    reform_simulation_id: Mapped[UUID | None] = mapped_column(Uuid)
    household_id: Mapped[UUID | None] = mapped_column(Uuid)
    dataset_id: Mapped[UUID | None] = mapped_column(Uuid)
    region_id: Mapped[UUID | None] = mapped_column(Uuid)
    year: Mapped[int | None] = mapped_column(Integer)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON)
    # The number of years from ``year`` an economy report's window covers.
    budget_window_size: Mapped[int | None] = mapped_column(Integer)
    key_version: Mapped[int | None] = _key_version_column()
    definition_key: Mapped[str | None] = _key_column()
    dataset_independent_key: Mapped[str | None] = _key_column()


class SimulationRun(ServiceBase):
    """One calculation of a simulation for one year and set of versions."""

    __tablename__ = "simulation_runs"
    __table_args__: ClassVar = (
        PrimaryKeyConstraint("id", name="pk_simulation_runs"),
        _api_v1_key(
            "tax_benefit_model_version_id",
            ReferencedTaxBenefitModelVersion.id,
            ondelete="RESTRICT",
            name="fk_simulation_runs_tax_benefit_model_version_id_tax_ben_1a94",
        ),
        _api_v1_key(
            "dataset_id",
            ReferencedDataset.id,
            ondelete="RESTRICT",
            name="fk_simulation_runs_dataset_id_datasets",
        ),
        _api_v1_key(
            "region_id",
            ReferencedRegion.id,
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
        _completion_required("simulation_runs"),
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

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    updated_at: Mapped[datetime] = server_timestamp_column()
    simulation_id: Mapped[UUID] = mapped_column(Uuid)
    # The year it simulates; a budget window runs a simulation once per year.
    year: Mapped[int] = mapped_column(Integer)
    tax_benefit_model_version_id: Mapped[UUID] = mapped_column(Uuid)
    country_package_version: Mapped[str] = mapped_column(String(VERSION_LENGTH))
    policyengine_version: Mapped[str] = mapped_column(String(VERSION_LENGTH))
    dataset_id: Mapped[UUID | None] = mapped_column(Uuid)
    region_id: Mapped[UUID | None] = mapped_column(Uuid)
    status: Mapped[SimulationStatus] = mapped_column(
        enum_type(SimulationStatus, "v2_simulation_status")
    )
    modal_call_id: Mapped[str | None] = mapped_column(String(MODAL_CALL_ID_LENGTH))
    error_message: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime | None] = optional_timestamp_column()
    completed_at: Mapped[datetime | None] = optional_timestamp_column()
    # The Modal worker writes None as SQL NULL here and in the two columns
    # below (HouseholdJob.result, ReportRun.spm_provenance), so a query for
    # IS NULL finds an empty result.
    household_result: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    output_uri: Mapped[str | None] = mapped_column(String(OUTPUT_URI_LENGTH))
    output_sha256: Mapped[str | None] = mapped_column(String(SHA256_LENGTH))
    forced: Mapped[bool] = _forced_column()


class BudgetWindowRun(ServiceBase):
    """One calculation of a report's budget window: a report run for each year."""

    __tablename__ = "budget_window_runs"
    __table_args__: ClassVar = (
        PrimaryKeyConstraint("id", name="pk_budget_window_runs"),
        _api_v1_key(
            "tax_benefit_model_version_id",
            ReferencedTaxBenefitModelVersion.id,
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
        _completion_required("budget_window_runs"),
        Index(
            "ix_budget_window_runs_report_id_created_at",
            "report_id",
            "created_at",
        ),
        Index(
            "ix_budget_window_runs_tax_benefit_model_version_id",
            "tax_benefit_model_version_id",
        ),
        # One window run that hasn't failed and wasn't forced per report and
        # version set: find-or-create's conflict target for a window (answer 8).
        Index(
            "uq_budget_window_runs_unfailed_unforced_versions",
            "report_id",
            "tax_benefit_model_version_id",
            "policyengine_version",
            unique=True,
            postgresql_where=text("status <> 'failed' AND NOT recalculate"),
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    updated_at: Mapped[datetime] = server_timestamp_column()
    report_id: Mapped[UUID] = mapped_column(Uuid)
    tax_benefit_model_version_id: Mapped[UUID] = mapped_column(Uuid)
    # The .py version the window calculates on, so a window run is found by
    # its report and version set (answer 8); null on runs before C1 · 27.
    policyengine_version: Mapped[str | None] = mapped_column(String(128))
    # Per-request, so the service no longer writes it (answer 6).
    trigger: Mapped[ReportRunTrigger | None] = mapped_column(
        enum_type(ReportRunTrigger, "v2_report_run_trigger")
    )
    # The window's forced marker: a recalculating run is never joined.
    recalculate: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[ReportRunStatus] = mapped_column(
        enum_type(ReportRunStatus, "v2_report_run_status")
    )
    error_message: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime | None] = optional_timestamp_column()
    completed_at: Mapped[datetime | None] = optional_timestamp_column()


class ReportRun(ServiceBase):
    """One calculation of a report, comparing a pair of simulation runs."""

    __tablename__ = "report_runs"
    __table_args__: ClassVar = (
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
        # The window run belongs to the same report, and deleting it deletes
        # its year runs.
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
        _completion_required("report_runs"),
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

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    updated_at: Mapped[datetime] = server_timestamp_column()
    report_id: Mapped[UUID] = mapped_column(Uuid)
    country_package_version: Mapped[str] = mapped_column(String(VERSION_LENGTH))
    policyengine_version: Mapped[str] = mapped_column(String(VERSION_LENGTH))
    status: Mapped[ReportRunStatus] = mapped_column(
        enum_type(ReportRunStatus, "v2_report_run_status")
    )
    # Per-request, so the service no longer writes it (answer 6).
    trigger: Mapped[ReportRunTrigger | None] = mapped_column(
        enum_type(ReportRunTrigger, "v2_report_run_trigger")
    )
    idempotency_key: Mapped[UUID | None] = mapped_column(Uuid)
    started_at: Mapped[datetime | None] = optional_timestamp_column()
    completed_at: Mapped[datetime | None] = optional_timestamp_column()
    error_message: Mapped[str | None] = mapped_column(String)
    markdown: Mapped[str | None] = mapped_column(Text)
    # The pair it compares; a household report with only a baseline has no
    # reform run.
    baseline_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    reform_simulation_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    # Set on each year's run of a budget window, with that year.
    budget_window_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    year: Mapped[int | None] = mapped_column(Integer)
    modal_call_id: Mapped[str | None] = mapped_column(String(MODAL_CALL_ID_LENGTH))
    # Cliff impact: all four values or none.
    baseline_cliff_gap: Mapped[float | None] = mapped_column(Float)
    baseline_cliff_share: Mapped[float | None] = mapped_column(Float)
    reform_cliff_gap: Mapped[float | None] = mapped_column(Float)
    reform_cliff_share: Mapped[float | None] = mapped_column(Float)
    spm_provenance: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    data_version: Mapped[str | None] = mapped_column(String(VERSION_LENGTH))
    forced: Mapped[bool] = _forced_column()


class HouseholdJob(ServiceBase):
    """One household calculation."""

    __tablename__ = "household_jobs"
    __table_args__: ClassVar = (
        PrimaryKeyConstraint("id", name="pk_household_jobs"),
        _api_v1_key(
            "household_id",
            ReferencedHousehold.id,
            ondelete="RESTRICT",
            name="fk_household_jobs_household_id_households",
        ),
        _api_v1_key(
            "policy_id",
            ReferencedPolicy.id,
            ondelete="SET NULL",
            name="fk_household_jobs_policy_id_policies",
        ),
        _api_v1_key(
            "dynamic_id",
            ReferencedDynamic.id,
            ondelete="SET NULL",
            name="fk_household_jobs_dynamic_id_dynamics",
        ),
        _api_v1_key(
            "tax_benefit_model_version_id",
            ReferencedTaxBenefitModelVersion.id,
            ondelete="RESTRICT",
            name="fk_household_jobs_tax_benefit_model_version_id_tax_bene_82b2",
        ),
        _completion_required("household_jobs"),
        _year_between("household_jobs", nullable=False),
        Index("ix_household_jobs_household_id", "household_id"),
        Index("ix_household_jobs_status_created_at", "status", "created_at"),
        Index(
            "ix_household_jobs_tax_benefit_model_version_id",
            "tax_benefit_model_version_id",
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    country: Mapped[str] = mapped_column(String(16))
    request_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    policy_id: Mapped[UUID | None] = mapped_column(Uuid)
    dynamic_id: Mapped[UUID | None] = mapped_column(Uuid)
    status: Mapped[HouseholdJobStatus] = mapped_column(
        enum_type(HouseholdJobStatus, "v2_household_job_status")
    )
    error_message: Mapped[str | None] = mapped_column(String)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    started_at: Mapped[datetime | None] = optional_timestamp_column()
    completed_at: Mapped[datetime | None] = optional_timestamp_column()
    updated_at: Mapped[datetime] = server_timestamp_column()
    household_id: Mapped[UUID] = mapped_column(Uuid)
    year: Mapped[int] = mapped_column(Integer)
    tax_benefit_model_version_id: Mapped[UUID] = mapped_column(Uuid)
    country_package_version: Mapped[str] = mapped_column(String(VERSION_LENGTH))
    policyengine_version: Mapped[str] = mapped_column(String(VERSION_LENGTH))
    modal_call_id: Mapped[str | None] = mapped_column(String(MODAL_CALL_ID_LENGTH))


service_metadata = ServiceBase.metadata
