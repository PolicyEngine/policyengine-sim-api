"""Mapped classes of API v1's result tables, which the Modal worker role writes.

API v1's v2 migration chain creates these tables; this module only describes
them, so the v2 Modal runtime can insert typed rows. Each class matches what
the chain builds, column for column, with its constraints under the same names.
They derive from ``ResultBase``, apart from the tables the simulation service
creates rows in (``ServiceBase``), whose run tables their keys name.

Results that compare a report run's baseline and reform belong to the report
run, and name its two simulation runs; poverty and inequality, which describe
one simulation, belong to a simulation run. Deleting a run deletes its
results; a simulation run that a report run's results name can't be deleted.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # SQLAlchemy reads Mapped annotations
from enum import StrEnum
from typing import ClassVar, Final
from uuid import UUID  # noqa: TC003  # SQLAlchemy reads Mapped annotations at run time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from policyengine_simulation_persistence.orm_base import (
    ResultBase,
    enum_type,
    identifier_column,
    server_timestamp_column,
)
from policyengine_simulation_persistence.tables import ReportRun, SimulationRun

NAME_LENGTH: Final = 512
ENTITY_LENGTH: Final = 128
AREA_CODE_LENGTH: Final = 64
AREA_NAME_LENGTH: Final = 255


class DecileType(StrEnum):
    """Whether deciles rank households by income or by wealth."""

    INCOME = "income"
    WEALTH = "wealth"


class UKAreaType(StrEnum):
    """The level of UK area a local-area breakdown reports."""

    CONSTITUENCY = "constituency"
    LOCAL_AUTHORITY = "local_authority"


def _primary_key(table_name: str) -> PrimaryKeyConstraint:
    return PrimaryKeyConstraint("id", name=f"pk_{table_name}")


def _report_run_key(table_name: str) -> ForeignKeyConstraint:
    """Return the key of a result to the report run it belongs to."""
    return ForeignKeyConstraint(
        ["report_run_id"],
        [ReportRun.id],
        name=f"fk_{table_name}_report_run_id_report_runs",
        ondelete="CASCADE",
    )


def _compared_run_keys(
    table_name: str,
    *,
    baseline_key_name: str | None = None,
    reform_key_name: str | None = None,
) -> tuple[ForeignKeyConstraint, ForeignKeyConstraint]:
    """Return the keys of a result to the two simulation runs it compares.

    The key names are API v1's, which its chain shortens past PostgreSQL's 63
    characters and ends in four characters of their hash; a shortened name is
    passed in.
    """
    return (
        ForeignKeyConstraint(
            ["baseline_simulation_run_id"],
            [SimulationRun.id],
            name=baseline_key_name
            or f"fk_{table_name}_baseline_simulation_run_id_simulation_runs",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["reform_simulation_run_id"],
            [SimulationRun.id],
            name=reform_key_name
            or f"fk_{table_name}_reform_simulation_run_id_simulation_runs",
            ondelete="RESTRICT",
        ),
    )


def _simulation_run_key(table_name: str) -> ForeignKeyConstraint:
    """Return the key of a result to the simulation run it belongs to."""
    return ForeignKeyConstraint(
        ["simulation_run_id"],
        [SimulationRun.id],
        name=f"fk_{table_name}_simulation_run_id_simulation_runs",
        ondelete="CASCADE",
    )


def _disclosure_tier_check(table_name: str) -> CheckConstraint:
    """Allow a UK area's tier to be ``reported`` or ``approximate``, or empty.

    ``approximate`` marks a thin sample. The tier and the bounds of the area's
    income changes, each change's 90% interval, are all null without disclosure
    control.
    """
    return CheckConstraint(
        "disclosure_tier IS NULL OR disclosure_tier IN ('reported', 'approximate')",
        name=f"ck_{table_name}_disclosure_tier",
    )


class LaborSupplyResponse(ResultBase):
    """A report run's overall labour supply response."""

    __tablename__ = "labor_supply_responses"
    __table_args__: ClassVar = (
        _primary_key("labor_supply_responses"),
        _report_run_key("labor_supply_responses"),
        UniqueConstraint("report_run_id", name="uq_labor_supply_responses_run"),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    report_run_id: Mapped[UUID] = mapped_column(Uuid)
    income_effect_earnings_change: Mapped[float] = mapped_column(Float)
    substitution_effect_earnings_change: Mapped[float] = mapped_column(Float)
    total_earnings_change: Mapped[float] = mapped_column(Float)
    revenue_change: Mapped[float] = mapped_column(Float)
    relative_income_effect: Mapped[float] = mapped_column(Float)
    relative_substitution_effect: Mapped[float] = mapped_column(Float)
    baseline_weekly_hours: Mapped[float] = mapped_column(Float)
    reform_weekly_hours: Mapped[float] = mapped_column(Float)
    weekly_hours_change: Mapped[float] = mapped_column(Float)
    weekly_hours_income_effect: Mapped[float] = mapped_column(Float)
    weekly_hours_substitution_effect: Mapped[float] = mapped_column(Float)


class LaborSupplyDecileResponse(ResultBase):
    """One decile's labour supply response in a report run."""

    __tablename__ = "labor_supply_decile_responses"
    __table_args__: ClassVar = (
        _primary_key("labor_supply_decile_responses"),
        _report_run_key("labor_supply_decile_responses"),
        UniqueConstraint(
            "report_run_id",
            "decile",
            name="uq_labor_supply_decile_responses_run_decile",
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    report_run_id: Mapped[UUID] = mapped_column(Uuid)
    decile: Mapped[int] = mapped_column(Integer)
    average_income_effect: Mapped[float | None] = mapped_column(Float)
    average_substitution_effect: Mapped[float | None] = mapped_column(Float)
    relative_income_effect: Mapped[float | None] = mapped_column(Float)
    relative_substitution_effect: Mapped[float | None] = mapped_column(Float)


class BudgetSummary(ResultBase):
    """One variable's budget totals under a report run's baseline and reform."""

    __tablename__ = "budget_summary"
    __table_args__: ClassVar = (
        _primary_key("budget_summary"),
        _report_run_key("budget_summary"),
        *_compared_run_keys("budget_summary"),
        UniqueConstraint(
            "report_run_id",
            "variable_name",
            "entity",
            name="uq_budget_summary_run_variable_entity",
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    report_run_id: Mapped[UUID] = mapped_column(Uuid)
    baseline_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    reform_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    variable_name: Mapped[str] = mapped_column(String(NAME_LENGTH))
    entity: Mapped[str] = mapped_column(String(ENTITY_LENGTH))
    baseline_total: Mapped[float | None] = mapped_column(Float)
    reform_total: Mapped[float | None] = mapped_column(Float)
    change: Mapped[float | None] = mapped_column(Float)


class DecileImpact(ResultBase):
    """One decile's change in a report run.

    ``decile`` runs from 1 to ``quantiles``; rows without a decile variable
    count as equal.
    """

    __tablename__ = "decile_impacts"
    __table_args__: ClassVar = (
        _primary_key("decile_impacts"),
        _report_run_key("decile_impacts"),
        *_compared_run_keys("decile_impacts"),
        UniqueConstraint(
            "report_run_id",
            "income_variable",
            "decile_variable",
            "entity",
            "decile",
            "quantiles",
            name="uq_decile_impacts_run_measure",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("quantiles > 0", name="ck_decile_impacts_quantiles"),
        CheckConstraint(
            "decile BETWEEN 1 AND quantiles", name="ck_decile_impacts_decile"
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    report_run_id: Mapped[UUID] = mapped_column(Uuid)
    baseline_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    reform_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    income_variable: Mapped[str] = mapped_column(String(NAME_LENGTH))
    decile_variable: Mapped[str | None] = mapped_column(String(NAME_LENGTH))
    entity: Mapped[str] = mapped_column(String(ENTITY_LENGTH))
    decile: Mapped[int] = mapped_column(Integer)
    quantiles: Mapped[int] = mapped_column(Integer)
    baseline_mean: Mapped[float | None] = mapped_column(Float)
    reform_mean: Mapped[float | None] = mapped_column(Float)
    absolute_change: Mapped[float | None] = mapped_column(Float)
    relative_change: Mapped[float | None] = mapped_column(Float)
    count_better_off: Mapped[float | None] = mapped_column(Float)
    count_worse_off: Mapped[float | None] = mapped_column(Float)
    count_no_change: Mapped[float | None] = mapped_column(Float)


class IntraDecileImpact(ResultBase):
    """The shares of one decile that gain or lose, in a report run."""

    __tablename__ = "intra_decile_impacts"
    __table_args__: ClassVar = (
        _primary_key("intra_decile_impacts"),
        _report_run_key("intra_decile_impacts"),
        *_compared_run_keys(
            "intra_decile_impacts",
            baseline_key_name=(
                "fk_intra_decile_impacts_baseline_simulation_run_id_simu_7cf0"
            ),
            reform_key_name=(
                "fk_intra_decile_impacts_reform_simulation_run_id_simula_467e"
            ),
        ),
        UniqueConstraint(
            "report_run_id",
            "decile_type",
            "decile",
            name="uq_intra_decile_impacts_run_type_decile",
        ),
        CheckConstraint(
            "decile BETWEEN 0 AND 10",
            name="ck_intra_decile_impacts_decile",
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    report_run_id: Mapped[UUID] = mapped_column(Uuid)
    baseline_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    reform_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    decile_type: Mapped[DecileType] = mapped_column(
        enum_type(DecileType, "v2_decile_type")
    )
    decile: Mapped[int] = mapped_column(Integer)
    lose_more_than_5pct: Mapped[float | None] = mapped_column(Float)
    lose_less_than_5pct: Mapped[float | None] = mapped_column(Float)
    no_change: Mapped[float | None] = mapped_column(Float)
    gain_less_than_5pct: Mapped[float | None] = mapped_column(Float)
    gain_more_than_5pct: Mapped[float | None] = mapped_column(Float)


class ProgramStatistics(ResultBase):
    """One program's totals in a report run; rows without an entity count as equal."""

    __tablename__ = "program_statistics"
    __table_args__: ClassVar = (
        _primary_key("program_statistics"),
        _report_run_key("program_statistics"),
        *_compared_run_keys(
            "program_statistics",
            baseline_key_name=(
                "fk_program_statistics_baseline_simulation_run_id_simula_faf9"
            ),
        ),
        UniqueConstraint(
            "report_run_id",
            "program_name",
            "entity",
            name="uq_program_statistics_run_program_entity",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    report_run_id: Mapped[UUID] = mapped_column(Uuid)
    baseline_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    reform_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    program_name: Mapped[str] = mapped_column(String(NAME_LENGTH))
    entity: Mapped[str | None] = mapped_column(String(ENTITY_LENGTH))
    is_tax: Mapped[bool | None] = mapped_column(Boolean)
    baseline_total: Mapped[float | None] = mapped_column(Float)
    reform_total: Mapped[float | None] = mapped_column(Float)
    change: Mapped[float | None] = mapped_column(Float)
    baseline_count: Mapped[float | None] = mapped_column(Float)
    reform_count: Mapped[float | None] = mapped_column(Float)
    winners: Mapped[float | None] = mapped_column(Float)
    losers: Mapped[float | None] = mapped_column(Float)


class CongressionalDistrictImpact(ResultBase):
    """One US congressional district's change in a report run."""

    __tablename__ = "congressional_district_impacts"
    __table_args__: ClassVar = (
        _primary_key("congressional_district_impacts"),
        _report_run_key("congressional_district_impacts"),
        *_compared_run_keys(
            "congressional_district_impacts",
            baseline_key_name=(
                "fk_congressional_district_impacts_baseline_simulation_r_9f79"
            ),
            reform_key_name=(
                "fk_congressional_district_impacts_reform_simulation_run_80f5"
            ),
        ),
        UniqueConstraint(
            "report_run_id",
            "district_geoid",
            name="uq_congressional_district_impacts_run_geoid",
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    report_run_id: Mapped[UUID] = mapped_column(Uuid)
    baseline_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    reform_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    average_household_income_change: Mapped[float] = mapped_column(Float)
    relative_household_income_change: Mapped[float] = mapped_column(Float)
    population: Mapped[float] = mapped_column(Float)
    district_geoid: Mapped[int] = mapped_column(Integer)
    state_fips: Mapped[int] = mapped_column(Integer)
    district_number: Mapped[int] = mapped_column(Integer)
    winner_percentage: Mapped[float] = mapped_column(Float)
    loser_percentage: Mapped[float] = mapped_column(Float)
    no_change_percentage: Mapped[float] = mapped_column(Float)


class ConstituencyImpact(ResultBase):
    """One UK constituency's change in a report run."""

    __tablename__ = "constituency_impacts"
    __table_args__: ClassVar = (
        _primary_key("constituency_impacts"),
        _report_run_key("constituency_impacts"),
        *_compared_run_keys(
            "constituency_impacts",
            baseline_key_name=(
                "fk_constituency_impacts_baseline_simulation_run_id_simu_d2cd"
            ),
            reform_key_name=(
                "fk_constituency_impacts_reform_simulation_run_id_simula_aaa1"
            ),
        ),
        _disclosure_tier_check("constituency_impacts"),
        UniqueConstraint(
            "report_run_id",
            "constituency_code",
            name="uq_constituency_impacts_run_code",
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    report_run_id: Mapped[UUID] = mapped_column(Uuid)
    baseline_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    reform_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    average_household_income_change: Mapped[float] = mapped_column(Float)
    relative_household_income_change: Mapped[float] = mapped_column(Float)
    population: Mapped[float] = mapped_column(Float)
    disclosure_tier: Mapped[str | None] = mapped_column(String(16))
    average_household_income_change_lower: Mapped[float | None] = mapped_column(Float)
    average_household_income_change_upper: Mapped[float | None] = mapped_column(Float)
    relative_household_income_change_lower: Mapped[float | None] = mapped_column(Float)
    relative_household_income_change_upper: Mapped[float | None] = mapped_column(Float)
    constituency_code: Mapped[str] = mapped_column(String(AREA_CODE_LENGTH))
    constituency_name: Mapped[str] = mapped_column(String(AREA_NAME_LENGTH))


class LocalAuthorityImpact(ResultBase):
    """One UK local authority's change in a report run."""

    __tablename__ = "local_authority_impacts"
    __table_args__: ClassVar = (
        _primary_key("local_authority_impacts"),
        _report_run_key("local_authority_impacts"),
        *_compared_run_keys(
            "local_authority_impacts",
            baseline_key_name=(
                "fk_local_authority_impacts_baseline_simulation_run_id_s_0d31"
            ),
            reform_key_name=(
                "fk_local_authority_impacts_reform_simulation_run_id_sim_0dc2"
            ),
        ),
        _disclosure_tier_check("local_authority_impacts"),
        UniqueConstraint(
            "report_run_id",
            "local_authority_code",
            name="uq_local_authority_impacts_run_code",
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    report_run_id: Mapped[UUID] = mapped_column(Uuid)
    baseline_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    reform_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    average_household_income_change: Mapped[float] = mapped_column(Float)
    relative_household_income_change: Mapped[float] = mapped_column(Float)
    population: Mapped[float] = mapped_column(Float)
    disclosure_tier: Mapped[str | None] = mapped_column(String(16))
    average_household_income_change_lower: Mapped[float | None] = mapped_column(Float)
    average_household_income_change_upper: Mapped[float | None] = mapped_column(Float)
    relative_household_income_change_lower: Mapped[float | None] = mapped_column(Float)
    relative_household_income_change_upper: Mapped[float | None] = mapped_column(Float)
    local_authority_code: Mapped[str] = mapped_column(String(AREA_CODE_LENGTH))
    local_authority_name: Mapped[str] = mapped_column(String(AREA_NAME_LENGTH))


class WithheldAreaCode(ResultBase):
    """A UK area a local-area run estimated but disclosure control withheld.

    There is one row per area; none means none were withheld.
    """

    __tablename__ = "withheld_area_codes"
    __table_args__: ClassVar = (
        _primary_key("withheld_area_codes"),
        _report_run_key("withheld_area_codes"),
        *_compared_run_keys(
            "withheld_area_codes",
            baseline_key_name=(
                "fk_withheld_area_codes_baseline_simulation_run_id_simul_a169"
            ),
        ),
        UniqueConstraint(
            "report_run_id",
            "area_type",
            "code",
            name="uq_withheld_area_codes_run_type_code",
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    report_run_id: Mapped[UUID] = mapped_column(Uuid)
    baseline_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    reform_simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    area_type: Mapped[UKAreaType] = mapped_column(
        enum_type(UKAreaType, "v2_uk_area_type")
    )
    code: Mapped[str] = mapped_column(String(AREA_CODE_LENGTH))


class Poverty(ResultBase):
    """One poverty measure of a simulation run.

    Rows without a filter variable or group count as equal, so a run has one
    unfiltered row per poverty type and entity.
    """

    __tablename__ = "poverty"
    __table_args__: ClassVar = (
        _primary_key("poverty"),
        _simulation_run_key("poverty"),
        UniqueConstraint(
            "simulation_run_id",
            "poverty_type",
            "entity",
            "filter_variable",
            "filter_group",
            name="uq_poverty_simulation_run_measure",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    poverty_type: Mapped[str] = mapped_column(String(ENTITY_LENGTH))
    entity: Mapped[str] = mapped_column(String(ENTITY_LENGTH))
    filter_variable: Mapped[str | None] = mapped_column(String(NAME_LENGTH))
    filter_group: Mapped[str | None] = mapped_column(String(64))
    headcount: Mapped[float | None] = mapped_column(Float)
    total_population: Mapped[float | None] = mapped_column(Float)
    rate: Mapped[float | None] = mapped_column(Float)


class Inequality(ResultBase):
    """One income variable's inequality measures in a simulation run."""

    __tablename__ = "inequality"
    __table_args__: ClassVar = (
        _primary_key("inequality"),
        _simulation_run_key("inequality"),
        UniqueConstraint(
            "simulation_run_id",
            "income_variable",
            "entity",
            name="uq_inequality_simulation_run_measure",
        ),
    )

    id: Mapped[UUID] = identifier_column()
    created_at: Mapped[datetime] = server_timestamp_column()
    simulation_run_id: Mapped[UUID] = mapped_column(Uuid)
    income_variable: Mapped[str] = mapped_column(String(NAME_LENGTH))
    entity: Mapped[str] = mapped_column(String(ENTITY_LENGTH))
    gini: Mapped[float | None] = mapped_column(Float)
    top_10_share: Mapped[float | None] = mapped_column(Float)
    top_1_share: Mapped[float | None] = mapped_column(Float)
    bottom_50_share: Mapped[float | None] = mapped_column(Float)


result_metadata = ResultBase.metadata
RESULT_TABLE_NAMES: Final = tuple(
    sorted(mapper.class_.__tablename__ for mapper in ResultBase.registry.mappers)
)

# The Core tables the stores' statements use until they name these classes.
labor_supply_responses = LaborSupplyResponse.__table__
labor_supply_decile_responses = LaborSupplyDecileResponse.__table__
budget_summary = BudgetSummary.__table__
decile_impacts = DecileImpact.__table__
intra_decile_impacts = IntraDecileImpact.__table__
program_statistics = ProgramStatistics.__table__
congressional_district_impacts = CongressionalDistrictImpact.__table__
constituency_impacts = ConstituencyImpact.__table__
local_authority_impacts = LocalAuthorityImpact.__table__
withheld_area_codes = WithheldAreaCode.__table__
poverty = Poverty.__table__
inequality = Inequality.__table__
