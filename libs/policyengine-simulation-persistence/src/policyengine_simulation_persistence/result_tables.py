"""Models of the Public API's result tables, which the Modal worker role writes.

The Public API's v2 migration chain creates these tables; this module only
describes them, so the v2 Modal runtime can insert typed rows. Each table
matches what the chain builds, column for column, with its constraints under
the same names. They are in ``result_metadata``, apart from the tables the
simulation service creates rows in (``service_metadata``), whose run tables
their keys name.

Results that compare a report run's baseline and reform belong to the report
run, and name its two simulation runs; poverty and inequality, which describe
one simulation, belong to a simulation run. Deleting a run deletes its
results; a simulation run that a report run's results name can't be deleted.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)

from policyengine_simulation_persistence.tables import (
    TABLE_SCHEMA,
    _enum_type,
    _identifier,
    _timestamp,
    report_runs,
    simulation_runs,
)

if TYPE_CHECKING:
    from sqlalchemy.sql.schema import SchemaItem

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


result_metadata = MetaData(schema=TABLE_SCHEMA)


def _result_columns(table_name: str) -> tuple[SchemaItem, ...]:
    """Return every result's ``id`` and ``created_at``, and its primary key."""
    return (
        _identifier(),
        _timestamp("created_at", server_now=True),
        PrimaryKeyConstraint("id", name=f"pk_{table_name}"),
    )


def _of_report_run(
    table_name: str,
    *,
    baseline_key_name: str | None = None,
    reform_key_name: str | None = None,
) -> tuple[SchemaItem, ...]:
    """Return the keys of a result of a report run that compares its two runs.

    The key names are the Public API's, which its chain shortens past
    PostgreSQL's 63 characters and ends in four characters of their hash; a
    shortened name is passed in.
    """
    return (
        *_of_report_run_alone(table_name),
        Column("baseline_simulation_run_id", Uuid, nullable=False),
        Column("reform_simulation_run_id", Uuid, nullable=False),
        ForeignKeyConstraint(
            ["baseline_simulation_run_id"],
            [simulation_runs.c.id],
            name=baseline_key_name
            or f"fk_{table_name}_baseline_simulation_run_id_simulation_runs",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["reform_simulation_run_id"],
            [simulation_runs.c.id],
            name=reform_key_name
            or f"fk_{table_name}_reform_simulation_run_id_simulation_runs",
            ondelete="RESTRICT",
        ),
    )


def _of_report_run_alone(table_name: str) -> tuple[SchemaItem, ...]:
    """Return the key of a result of a report run that names no simulation run."""
    return (
        *_result_columns(table_name),
        Column("report_run_id", Uuid, nullable=False),
        ForeignKeyConstraint(
            ["report_run_id"],
            [report_runs.c.id],
            name=f"fk_{table_name}_report_run_id_report_runs",
            ondelete="CASCADE",
        ),
    )


def _of_simulation_run(table_name: str) -> tuple[SchemaItem, ...]:
    """Return the key of a result of one simulation run."""
    return (
        *_result_columns(table_name),
        Column("simulation_run_id", Uuid, nullable=False),
        ForeignKeyConstraint(
            ["simulation_run_id"],
            [simulation_runs.c.id],
            name=f"fk_{table_name}_simulation_run_id_simulation_runs",
            ondelete="CASCADE",
        ),
    )


def _values(*names: str, nullable: bool) -> tuple[Column[float], ...]:
    return tuple(Column(name, Float, nullable=nullable) for name in names)


def _area_values() -> tuple[Column[float], ...]:
    """Return a geographic impact's household income changes and population."""
    return _values(
        "average_household_income_change",
        "relative_household_income_change",
        "population",
        nullable=False,
    )


def _disclosure(table_name: str) -> tuple[SchemaItem, ...]:
    """Return a UK area's disclosure tier and the bounds of its income changes.

    The tier is ``reported``, or ``approximate`` for a thin sample; the bounds
    are each income change's 90% interval. All are null without disclosure
    control.
    """
    return (
        Column("disclosure_tier", String(16), nullable=True),
        *_values(
            "average_household_income_change_lower",
            "average_household_income_change_upper",
            "relative_household_income_change_lower",
            "relative_household_income_change_upper",
            nullable=True,
        ),
        CheckConstraint(
            "disclosure_tier IS NULL OR disclosure_tier IN ('reported', 'approximate')",
            name=f"ck_{table_name}_disclosure_tier",
        ),
    )


labor_supply_responses = Table(
    "labor_supply_responses",
    result_metadata,
    *_of_report_run_alone("labor_supply_responses"),
    *_values(
        "income_effect_earnings_change",
        "substitution_effect_earnings_change",
        "total_earnings_change",
        "revenue_change",
        "relative_income_effect",
        "relative_substitution_effect",
        "baseline_weekly_hours",
        "reform_weekly_hours",
        "weekly_hours_change",
        "weekly_hours_income_effect",
        "weekly_hours_substitution_effect",
        nullable=False,
    ),
    UniqueConstraint("report_run_id", name="uq_labor_supply_responses_run"),
)

labor_supply_decile_responses = Table(
    "labor_supply_decile_responses",
    result_metadata,
    *_of_report_run_alone("labor_supply_decile_responses"),
    Column("decile", Integer, nullable=False),
    *_values(
        "average_income_effect",
        "average_substitution_effect",
        "relative_income_effect",
        "relative_substitution_effect",
        nullable=True,
    ),
    UniqueConstraint(
        "report_run_id",
        "decile",
        name="uq_labor_supply_decile_responses_run_decile",
    ),
)

budget_summary = Table(
    "budget_summary",
    result_metadata,
    *_of_report_run("budget_summary"),
    Column("variable_name", String(NAME_LENGTH), nullable=False),
    Column("entity", String(ENTITY_LENGTH), nullable=False),
    *_values("baseline_total", "reform_total", "change", nullable=True),
    UniqueConstraint(
        "report_run_id",
        "variable_name",
        "entity",
        name="uq_budget_summary_run_variable_entity",
    ),
)

# ``decile`` runs from 1 to ``quantiles``; rows without a decile variable
# count as equal.
decile_impacts = Table(
    "decile_impacts",
    result_metadata,
    *_of_report_run("decile_impacts"),
    Column("income_variable", String(NAME_LENGTH), nullable=False),
    Column("decile_variable", String(NAME_LENGTH), nullable=True),
    Column("entity", String(ENTITY_LENGTH), nullable=False),
    Column("decile", Integer, nullable=False),
    Column("quantiles", Integer, nullable=False),
    *_values(
        "baseline_mean",
        "reform_mean",
        "absolute_change",
        "relative_change",
        "count_better_off",
        "count_worse_off",
        "count_no_change",
        nullable=True,
    ),
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
    CheckConstraint("decile BETWEEN 1 AND quantiles", name="ck_decile_impacts_decile"),
)

intra_decile_impacts = Table(
    "intra_decile_impacts",
    result_metadata,
    *_of_report_run(
        "intra_decile_impacts",
        baseline_key_name="fk_intra_decile_impacts_baseline_simulation_run_id_simu_7cf0",
        reform_key_name="fk_intra_decile_impacts_reform_simulation_run_id_simula_467e",
    ),
    Column(
        "decile_type",
        _enum_type(DecileType, "v2_decile_type"),
        nullable=False,
    ),
    Column("decile", Integer, nullable=False),
    *_values(
        "lose_more_than_5pct",
        "lose_less_than_5pct",
        "no_change",
        "gain_less_than_5pct",
        "gain_more_than_5pct",
        nullable=True,
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

# Rows without an entity count as equal.
program_statistics = Table(
    "program_statistics",
    result_metadata,
    *_of_report_run(
        "program_statistics",
        baseline_key_name="fk_program_statistics_baseline_simulation_run_id_simula_faf9",
    ),
    Column("program_name", String(NAME_LENGTH), nullable=False),
    Column("entity", String(ENTITY_LENGTH), nullable=True),
    Column("is_tax", Boolean, nullable=True),
    *_values(
        "baseline_total",
        "reform_total",
        "change",
        "baseline_count",
        "reform_count",
        "winners",
        "losers",
        nullable=True,
    ),
    UniqueConstraint(
        "report_run_id",
        "program_name",
        "entity",
        name="uq_program_statistics_run_program_entity",
        postgresql_nulls_not_distinct=True,
    ),
)

congressional_district_impacts = Table(
    "congressional_district_impacts",
    result_metadata,
    *_of_report_run(
        "congressional_district_impacts",
        baseline_key_name="fk_congressional_district_impacts_baseline_simulation_r_9f79",
        reform_key_name="fk_congressional_district_impacts_reform_simulation_run_80f5",
    ),
    *_area_values(),
    Column("district_geoid", Integer, nullable=False),
    Column("state_fips", Integer, nullable=False),
    Column("district_number", Integer, nullable=False),
    *_values(
        "winner_percentage",
        "loser_percentage",
        "no_change_percentage",
        nullable=False,
    ),
    UniqueConstraint(
        "report_run_id",
        "district_geoid",
        name="uq_congressional_district_impacts_run_geoid",
    ),
)

constituency_impacts = Table(
    "constituency_impacts",
    result_metadata,
    *_of_report_run(
        "constituency_impacts",
        baseline_key_name="fk_constituency_impacts_baseline_simulation_run_id_simu_d2cd",
        reform_key_name="fk_constituency_impacts_reform_simulation_run_id_simula_aaa1",
    ),
    *_area_values(),
    *_disclosure("constituency_impacts"),
    Column("constituency_code", String(AREA_CODE_LENGTH), nullable=False),
    Column("constituency_name", String(AREA_NAME_LENGTH), nullable=False),
    UniqueConstraint(
        "report_run_id",
        "constituency_code",
        name="uq_constituency_impacts_run_code",
    ),
)

local_authority_impacts = Table(
    "local_authority_impacts",
    result_metadata,
    *_of_report_run(
        "local_authority_impacts",
        baseline_key_name="fk_local_authority_impacts_baseline_simulation_run_id_s_0d31",
        reform_key_name="fk_local_authority_impacts_reform_simulation_run_id_sim_0dc2",
    ),
    *_area_values(),
    *_disclosure("local_authority_impacts"),
    Column("local_authority_code", String(AREA_CODE_LENGTH), nullable=False),
    Column("local_authority_name", String(AREA_NAME_LENGTH), nullable=False),
    UniqueConstraint(
        "report_run_id",
        "local_authority_code",
        name="uq_local_authority_impacts_run_code",
    ),
)

# The codes of the UK areas a local-area run estimated but disclosure control
# withheld, one row per area; none means none were withheld.
withheld_area_codes = Table(
    "withheld_area_codes",
    result_metadata,
    *_of_report_run(
        "withheld_area_codes",
        baseline_key_name="fk_withheld_area_codes_baseline_simulation_run_id_simul_a169",
    ),
    Column("area_type", _enum_type(UKAreaType, "v2_uk_area_type"), nullable=False),
    Column("code", String(AREA_CODE_LENGTH), nullable=False),
    UniqueConstraint(
        "report_run_id",
        "area_type",
        "code",
        name="uq_withheld_area_codes_run_type_code",
    ),
)

# Rows without a filter variable or group count as equal, so a run has one
# unfiltered row per poverty type and entity.
poverty = Table(
    "poverty",
    result_metadata,
    *_of_simulation_run("poverty"),
    Column("poverty_type", String(ENTITY_LENGTH), nullable=False),
    Column("entity", String(ENTITY_LENGTH), nullable=False),
    Column("filter_variable", String(NAME_LENGTH), nullable=True),
    Column("filter_group", String(64), nullable=True),
    *_values("headcount", "total_population", "rate", nullable=True),
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

inequality = Table(
    "inequality",
    result_metadata,
    *_of_simulation_run("inequality"),
    Column("income_variable", String(NAME_LENGTH), nullable=False),
    Column("entity", String(ENTITY_LENGTH), nullable=False),
    *_values(
        "gini",
        "top_10_share",
        "top_1_share",
        "bottom_50_share",
        nullable=True,
    ),
    UniqueConstraint(
        "simulation_run_id",
        "income_variable",
        "entity",
        name="uq_inequality_simulation_run_measure",
    ),
)
