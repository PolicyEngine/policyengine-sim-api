"""The result tables: every economy result stored as rows, attached to its run.

Each section of today's economy result model (``SingleYearMacroOutput``) maps
to one table. Numbers that describe one simulation (a baseline's or a reform's
own poverty rates, inequality and cliff exposure) belong to that simulation
run, as computed by one comparison: they are keyed by the simulation run and
the report run, because a reused run's figures can differ between comparisons.
Comparisons (the change between baseline and reform) belong to the report
run. Lists and maps keep
their order in a ``position`` column, so a reader rebuilds the result exactly.

Constituency and local-authority names and map positions are not stored: they
come from the region catalog by code. Each area's disclosure tier and 90%
interval bounds are stored with its figures, null for a result computed before
disclosure control. The model and data versions the result reports, its SPM
provenance and the list of optional sections it holds are columns on the
report run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Column,
    Double,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)

from policyengine_simulation_persistence.runs import report_runs, simulation_runs
from policyengine_simulation_persistence.schema import simulation_metadata

if TYPE_CHECKING:
    from sqlalchemy.schema import SchemaItem

POVERTY_MEASURES = ("poverty", "deep_poverty")
POVERTY_GROUPS = {
    "age": ("child", "adult", "senior", "all"),
    "gender": ("male", "female"),
    "race": ("white", "black", "hispanic", "other"),
}
DECILE_MEASURES = ("income", "wealth")
# The contract's UKGeographyDisclosureTier values, written out so that a new
# tier fails a test rather than silently changing the declared table.
UK_AREA_DISCLOSURE_TIERS = ("reported", "approximate")
UK_AREA_DISCLOSURE_COLUMNS = (
    "disclosure_tier",
    "average_household_income_change_lower",
    "average_household_income_change_upper",
    "relative_household_income_change_lower",
    "relative_household_income_change_upper",
)


def _values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _simulation_run_key(table_name: str) -> list[SchemaItem]:
    """Key a simulation's own figures by its run and the comparison that computed them.

    A reused simulation run takes part in many comparisons, and its figures
    can differ between them (US poverty rates follow each comparison's SPM
    selection), so each report run keeps its own copy.
    """
    return [
        Column("report_run_id", Uuid(as_uuid=True), nullable=False),
        Column("simulation_run_id", Uuid(as_uuid=True), nullable=False),
        # Which side of the comparison the figures describe; it tells them
        # apart when one simulation run is both the baseline and the reform.
        Column("side", Text, nullable=False),
        CheckConstraint("side IN ('baseline', 'reform')", name="side"),
        ForeignKeyConstraint(
            ["report_run_id"],
            [report_runs.c.id],
            name=f"fk_{table_name}_report_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["simulation_run_id"],
            [simulation_runs.c.id],
            name=f"fk_{table_name}_simulation_run",
            ondelete="RESTRICT",
        ),
        Index(f"ix_{table_name}_simulation_run_id", "simulation_run_id"),
    ]


def _report_run_key(table_name: str) -> list[SchemaItem]:
    return [
        Column("report_run_id", Uuid(as_uuid=True), nullable=False),
        ForeignKeyConstraint(
            ["report_run_id"],
            [report_runs.c.id],
            name=f"fk_{table_name}_report_run",
            ondelete="RESTRICT",
        ),
    ]


def _numbers(*names: str) -> list[Column[float]]:
    return [Column(name, Double, nullable=False) for name in names]


def _position() -> list[SchemaItem]:
    return [
        Column("position", Integer, nullable=False),
        CheckConstraint("position >= 0", name="position"),
    ]


def _uk_area_disclosure() -> list[SchemaItem]:
    """Return a UK area's disclosure tier and its changes' 90% interval bounds.

    All are null for a result computed before disclosure control.
    """
    tier_column, *bound_columns = UK_AREA_DISCLOSURE_COLUMNS
    return [
        Column(tier_column, Text, nullable=True),
        *(Column(name, Double, nullable=True) for name in bound_columns),
        CheckConstraint(
            f"{tier_column} IS NULL OR "
            f"{tier_column} IN ({_values(UK_AREA_DISCLOSURE_TIERS)})",
            name=tier_column,
        ),
    ]


# --- Per simulation run -----------------------------------------------------

poverty_rates = Table(
    "poverty_rates",
    simulation_metadata,
    *_simulation_run_key("poverty_rates"),
    Column("measure", Text, nullable=False),
    Column("group_dimension", Text, nullable=False),
    Column("group_value", Text, nullable=False),
    *_numbers("rate"),
    PrimaryKeyConstraint(
        "report_run_id",
        "simulation_run_id",
        "side",
        "measure",
        "group_dimension",
        "group_value",
    ),
    CheckConstraint(f"measure IN ({_values(POVERTY_MEASURES)})", name="measure"),
    CheckConstraint(
        " OR ".join(
            f"(group_dimension = '{dimension}' "
            f"AND group_value IN ({_values(group_values)}))"
            for dimension, group_values in POVERTY_GROUPS.items()
        ),
        name="group",
    ),
)

inequality = Table(
    "inequality",
    simulation_metadata,
    *_simulation_run_key("inequality"),
    *_numbers("gini", "top_10_pct_share", "top_1_pct_share"),
    PrimaryKeyConstraint("report_run_id", "simulation_run_id", "side"),
)

cliff_impacts = Table(
    "cliff_impacts",
    simulation_metadata,
    *_simulation_run_key("cliff_impacts"),
    *_numbers("cliff_gap", "cliff_share"),
    PrimaryKeyConstraint("report_run_id", "simulation_run_id", "side"),
)

# --- Per report run ---------------------------------------------------------

budget_impacts = Table(
    "budget_impacts",
    simulation_metadata,
    *_report_run_key("budget_impacts"),
    *_numbers(
        "budgetary_impact",
        "tax_revenue_impact",
        "state_tax_revenue_impact",
        "benefit_spending_impact",
        "households",
        "baseline_net_income",
    ),
    PrimaryKeyConstraint("report_run_id"),
)

program_budget_impacts = Table(
    "program_budget_impacts",
    simulation_metadata,
    *_report_run_key("program_budget_impacts"),
    Column("program", Text, nullable=False),
    *_position(),
    *_numbers("baseline", "reform", "difference"),
    PrimaryKeyConstraint("report_run_id", "program"),
    UniqueConstraint("report_run_id", "position"),
)

decile_impacts = Table(
    "decile_impacts",
    simulation_metadata,
    *_report_run_key("decile_impacts"),
    Column("measure", Text, nullable=False),
    # The decile's key as the result names it ("1" to "10").
    Column("decile", Text, nullable=False),
    *_position(),
    *_numbers("average_change", "relative_change"),
    PrimaryKeyConstraint("report_run_id", "measure", "decile"),
    UniqueConstraint("report_run_id", "measure", "position"),
    CheckConstraint(f"measure IN ({_values(DECILE_MEASURES)})", name="measure"),
)

intra_decile_impacts = Table(
    "intra_decile_impacts",
    simulation_metadata,
    *_report_run_key("intra_decile_impacts"),
    Column("measure", Text, nullable=False),
    # The size-of-change category, for example "Gain more than 5%".
    Column("outcome", Text, nullable=False),
    Column("outcome_position", Integer, nullable=False),
    # 1 to 10 for a decile's share; null for the average across deciles.
    Column("decile", SmallInteger, nullable=True),
    *_numbers("share"),
    UniqueConstraint(
        "report_run_id",
        "measure",
        "outcome",
        "decile",
        postgresql_nulls_not_distinct=True,
    ),
    CheckConstraint(f"measure IN ({_values(DECILE_MEASURES)})", name="measure"),
    CheckConstraint("decile IS NULL OR decile >= 1", name="decile"),
    CheckConstraint("outcome_position >= 0", name="outcome_position"),
)

labor_supply_responses = Table(
    "labor_supply_responses",
    simulation_metadata,
    *_report_run_key("labor_supply_responses"),
    *_numbers(
        "substitution_lsr",
        "income_lsr",
        "relative_income_response",
        "relative_substitution_response",
        "total_change",
        "revenue_change",
        "baseline_hours",
        "reform_hours",
        "hours_change",
        "hours_income_effect",
        "hours_substitution_effect",
    ),
    PrimaryKeyConstraint("report_run_id"),
)

# One row per decile any of the four maps names. The average maps may name a
# decile the relative maps leave out: policyengine.py puts households with
# negative income in decile -1 and gives relative effects for positive
# deciles only. Each pair keeps its own order.
labor_supply_decile_impacts = Table(
    "labor_supply_decile_impacts",
    simulation_metadata,
    *_report_run_key("labor_supply_decile_impacts"),
    Column("decile", Integer, nullable=False),
    Column("average_position", Integer, nullable=True),
    Column("relative_position", Integer, nullable=True),
    Column("average_income_effect", Double, nullable=True),
    Column("average_substitution_effect", Double, nullable=True),
    Column("relative_income_effect", Double, nullable=True),
    Column("relative_substitution_effect", Double, nullable=True),
    PrimaryKeyConstraint("report_run_id", "decile"),
    UniqueConstraint("report_run_id", "average_position"),
    UniqueConstraint("report_run_id", "relative_position"),
    CheckConstraint(
        "(average_position IS NULL) = (average_income_effect IS NULL) "
        "AND (average_position IS NULL) = (average_substitution_effect IS NULL)",
        name="average",
    ),
    CheckConstraint(
        "(relative_position IS NULL) = (relative_income_effect IS NULL) "
        "AND (relative_position IS NULL) = (relative_substitution_effect IS NULL)",
        name="relative",
    ),
    CheckConstraint(
        "average_position IS NOT NULL OR relative_position IS NOT NULL",
        name="in_a_map",
    ),
    CheckConstraint(
        "average_position >= 0 AND relative_position >= 0", name="positions"
    ),
)

congressional_district_impacts = Table(
    "congressional_district_impacts",
    simulation_metadata,
    *_report_run_key("congressional_district_impacts"),
    Column("district", Text, nullable=False),
    *_position(),
    *_numbers(
        "average_household_income_change",
        "relative_household_income_change",
        "winner_percentage",
        "loser_percentage",
        "no_change_percentage",
        "population",
    ),
    PrimaryKeyConstraint("report_run_id", "district"),
    UniqueConstraint("report_run_id", "position"),
)

constituency_impacts = Table(
    "constituency_impacts",
    simulation_metadata,
    *_report_run_key("constituency_impacts"),
    Column("constituency_code", Text, nullable=False),
    *_position(),
    *_numbers(
        "average_household_income_change",
        "relative_household_income_change",
        "population",
    ),
    *_uk_area_disclosure(),
    PrimaryKeyConstraint("report_run_id", "constituency_code"),
    UniqueConstraint("report_run_id", "position"),
)

local_authority_impacts = Table(
    "local_authority_impacts",
    simulation_metadata,
    *_report_run_key("local_authority_impacts"),
    Column("local_authority_code", Text, nullable=False),
    *_position(),
    *_numbers(
        "average_household_income_change",
        "relative_household_income_change",
        "population",
    ),
    *_uk_area_disclosure(),
    PrimaryKeyConstraint("report_run_id", "local_authority_code"),
    UniqueConstraint("report_run_id", "position"),
)

ECONOMY_RESULT_TABLES = (
    poverty_rates,
    inequality,
    cliff_impacts,
    budget_impacts,
    program_budget_impacts,
    decile_impacts,
    intra_decile_impacts,
    labor_supply_responses,
    labor_supply_decile_impacts,
    congressional_district_impacts,
    constituency_impacts,
    local_authority_impacts,
)
