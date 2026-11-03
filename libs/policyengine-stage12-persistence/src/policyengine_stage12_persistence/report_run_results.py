"""A report run's economy output as rows of the Public API's result tables.

The output is the single-year economy result Stage 12's coordinator builds,
in the format API v1 and v3 read today. Each part with a lossless place in a
result table becomes rows: per-report parts keyed by the report run and its
two simulation runs, poverty and inequality keyed by each simulation run. The
labour supply response, cliff impact and SPM receipts, which no result table
holds, are stored whole with the data version, in one ``report_run_comparisons``
row for the report run and its two simulation runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from sqlalchemy import JSON, Boolean, Column, Float, Integer, String, Table
from sqlalchemy.dialects.postgresql import ENUM, UUID

from policyengine_stage12_persistence.report_run_tables import metadata

if TYPE_CHECKING:
    from collections.abc import Mapping

    from policyengine_simulation_contract.macro_output import (
        BaselineReformValue,
        CongressionalDistrictImpactRecord,
        ConstituencyImpactRecord,
        DecileOutput,
        GeographicImpactOutput,
        IntraDecileOutput,
        LocalAuthorityImpactRecord,
    )
    from policyengine_simulation_contract.stage12_execution import ReportRunIds

decile_type_type = ENUM("income", "wealth", name="v2_decile_type", create_type=False)


def _report_run_keys() -> tuple[Column[Any], ...]:
    return (
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("report_run_id", UUID(as_uuid=True), nullable=False),
        Column("baseline_simulation_run_id", UUID(as_uuid=True), nullable=False),
        Column("reform_simulation_run_id", UUID(as_uuid=True), nullable=False),
    )


def _simulation_run_keys() -> tuple[Column[Any], ...]:
    return (
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("simulation_run_id", UUID(as_uuid=True), nullable=False),
    )


report_run_comparisons = Table(
    "report_run_comparisons",
    metadata,
    *_report_run_keys(),
    Column("labor_supply_result", JSON(none_as_null=True)),
    Column("cliff_impact_result", JSON(none_as_null=True)),
    Column("spm_provenance", JSON(none_as_null=True)),
    Column("data_version", String(128)),
)
program_statistics = Table(
    "program_statistics",
    metadata,
    *_report_run_keys(),
    Column("program_name", String(512), nullable=False),
    Column("entity", String(128)),
    Column("is_tax", Boolean),
    Column("baseline_total", Float),
    Column("reform_total", Float),
    Column("change", Float),
)


def _area_values() -> tuple[Column[Any], ...]:
    return (
        Column("average_household_income_change", Float, nullable=False),
        Column("relative_household_income_change", Float, nullable=False),
        Column("population", Float, nullable=False),
    )


def _disclosure_values() -> tuple[Column[Any], ...]:
    return (
        Column("disclosure_tier", String(16)),
        Column("average_household_income_change_lower", Float),
        Column("average_household_income_change_upper", Float),
        Column("relative_household_income_change_lower", Float),
        Column("relative_household_income_change_upper", Float),
    )


congressional_district_impacts = Table(
    "congressional_district_impacts",
    metadata,
    *_report_run_keys(),
    *_area_values(),
    Column("district_geoid", Integer, nullable=False),
    Column("state_fips", Integer, nullable=False),
    Column("district_number", Integer, nullable=False),
    Column("winner_percentage", Float, nullable=False),
    Column("loser_percentage", Float, nullable=False),
    Column("no_change_percentage", Float, nullable=False),
)
constituency_impacts = Table(
    "constituency_impacts",
    metadata,
    *_report_run_keys(),
    *_area_values(),
    *_disclosure_values(),
    Column("constituency_code", String(64), nullable=False),
    Column("constituency_name", String(255), nullable=False),
)
local_authority_impacts = Table(
    "local_authority_impacts",
    metadata,
    *_report_run_keys(),
    *_area_values(),
    *_disclosure_values(),
    Column("local_authority_code", String(64), nullable=False),
    Column("local_authority_name", String(255), nullable=False),
)
budget_summary = Table(
    "budget_summary",
    metadata,
    *_report_run_keys(),
    Column("variable_name", String(512), nullable=False),
    Column("entity", String(128), nullable=False),
    Column("baseline_total", Float),
    Column("reform_total", Float),
    Column("change", Float),
)
decile_impacts = Table(
    "decile_impacts",
    metadata,
    *_report_run_keys(),
    Column("income_variable", String(512), nullable=False),
    Column("decile_variable", String(512)),
    Column("entity", String(128), nullable=False),
    Column("decile", Integer, nullable=False),
    Column("quantiles", Integer, nullable=False),
    Column("absolute_change", Float),
    Column("relative_change", Float),
)
intra_decile_impacts = Table(
    "intra_decile_impacts",
    metadata,
    *_report_run_keys(),
    Column("decile_type", decile_type_type, nullable=False),
    Column("decile", Integer, nullable=False),
    Column("lose_more_than_5pct", Float),
    Column("lose_less_than_5pct", Float),
    Column("no_change", Float),
    Column("gain_less_than_5pct", Float),
    Column("gain_more_than_5pct", Float),
)
poverty = Table(
    "poverty",
    metadata,
    *_simulation_run_keys(),
    Column("poverty_type", String(128), nullable=False),
    Column("entity", String(128), nullable=False),
    Column("filter_variable", String(512)),
    Column("filter_group", String(64)),
    Column("rate", Float),
)
inequality = Table(
    "inequality",
    metadata,
    *_simulation_run_keys(),
    Column("income_variable", String(512), nullable=False),
    Column("entity", String(128), nullable=False),
    Column("gini", Float),
    Column("top_10_share", Float),
    Column("top_1_share", Float),
)

# The household variable each headline budget figure totals, and the column
# its figure goes in. ``budgetary_impact`` is tax minus benefits, as the
# economy output computes it, so it needs no row of its own.
BUDGET_FIGURES = (
    ("tax_revenue_impact", "household_tax", "change"),
    ("benefit_spending_impact", "household_benefits", "change"),
    ("state_tax_revenue_impact", "household_state_income_tax", "change"),
    ("baseline_net_income", "household_net_income", "baseline_total"),
    ("households", "household_weight", "baseline_total"),
)
# .py's poverty types behind the output's "poverty" and "deep_poverty".
POVERTY_TYPES = {
    "us": {"poverty": "spm", "deep_poverty": "spm_deep"},
    "uk": {"poverty": "relative_bhc", "deep_poverty": "absolute_bhc"},
}
# .py's poverty groups (``policyengine.outputs.poverty``), named as the
# output's parts name them; ``age``, ``is_male`` and ``race`` select them.
AGE_GROUPS = ("child", "adult", "senior")
GENDER_GROUPS = ("male", "female")
RACE_GROUPS = ("white", "black", "hispanic", "other")
# The variable .py groups UK wealth deciles' household net income by.
WEALTH_DECILE_VARIABLE = "household_wealth_decile"
INEQUALITY_INCOME_VARIABLES = {
    "us": "household_net_income",
    "uk": "equiv_hbai_household_net_income",
}
INTRA_DECILE_COLUMNS = {
    "Lose more than 5%": "lose_more_than_5pct",
    "Lose less than 5%": "lose_less_than_5pct",
    "No change": "no_change",
    "Gain less than 5%": "gain_less_than_5pct",
    "Gain more than 5%": "gain_more_than_5pct",
}
DISCLOSURE_FIELDS = (
    "disclosure_tier",
    "average_household_income_change_lower",
    "average_household_income_change_upper",
    "relative_household_income_change_lower",
    "relative_household_income_change_upper",
)


@dataclass(frozen=True)
class DistrictCodes:
    """US states' FIPS codes, and the states with one at-large district.

    Today's output names a district as ``"ST-NN"``, numbering an at-large
    district ``01``. Its row stores the Census GEOID parts instead: the
    state's FIPS code, and the district number, ``0`` for an at-large state.
    """

    state_fips: Mapping[str, int]
    at_large_states: frozenset[str]


@dataclass(frozen=True)
class ResultRows:
    """The rows one result table receives.

    Rows ``shared_by_reports`` belong to a simulation run, which other report
    runs may share; a row such a run already holds is kept as stored.
    """

    table: Table
    rows: tuple[dict[str, Any], ...]
    shared_by_reports: bool


@dataclass(frozen=True)
class ReportRunResult:
    """Everything a report run's economy output is stored as."""

    rows: tuple[ResultRows, ...]


def report_run_result(
    output: Mapping[str, Any],
    *,
    run_ids: ReportRunIds,
    country: str,
    district_codes: DistrictCodes | None = None,
) -> ReportRunResult:
    """Validate a single-year economy output and return the rows it is stored as.

    Raises ValueError for an output that is not a single-year economy result,
    a country without poverty types, or a congressional district whose state
    ``district_codes`` doesn't name.
    """
    economy = SingleYearMacroOutput.model_validate(output)
    if country not in POVERTY_TYPES:
        msg = f"no poverty types are known for country {country!r}"
        raise ValueError(msg)
    report_keys = {
        "report_run_id": run_ids.report_run_id,
        "baseline_simulation_run_id": run_ids.baseline_simulation_run_id,
        "reform_simulation_run_id": run_ids.reform_simulation_run_id,
    }
    dumped = economy.model_dump(mode="json")
    comparison = {
        "id": uuid4(),
        **report_keys,
        "labor_supply_result": dumped["labor_supply_response"],
        "cliff_impact_result": dumped["cliff_impact"],
        "spm_provenance": dumped["spm_provenance"],
        "data_version": economy.data_version,
    }
    return ReportRunResult(
        rows=(
            ResultRows(report_run_comparisons, (comparison,), shared_by_reports=False),
            ResultRows(
                budget_summary,
                _budget_rows(economy, report_keys, country),
                shared_by_reports=False,
            ),
            ResultRows(
                decile_impacts,
                _decile_rows(economy, report_keys),
                shared_by_reports=False,
            ),
            ResultRows(
                intra_decile_impacts,
                _intra_decile_rows(economy, report_keys),
                shared_by_reports=False,
            ),
            ResultRows(
                poverty,
                _poverty_rows(economy, run_ids, country),
                shared_by_reports=True,
            ),
            ResultRows(
                inequality,
                _inequality_rows(economy, run_ids, country),
                shared_by_reports=True,
            ),
            ResultRows(
                program_statistics,
                _program_rows(economy, report_keys),
                shared_by_reports=False,
            ),
            ResultRows(
                congressional_district_impacts,
                _district_rows(economy, report_keys, district_codes),
                shared_by_reports=False,
            ),
            ResultRows(
                constituency_impacts,
                _area_rows(economy.constituency_impact, report_keys, "constituency"),
                shared_by_reports=False,
            ),
            ResultRows(
                local_authority_impacts,
                _area_rows(
                    economy.local_authority_impact, report_keys, "local_authority"
                ),
                shared_by_reports=False,
            ),
        ),
    )


def _program_rows(
    economy: SingleYearMacroOutput, keys: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    # The output names no program's entity, so every program's is null.
    return tuple(
        {
            "id": uuid4(),
            **keys,
            "program_name": program_name,
            "entity": None,
            "is_tax": program.is_tax,
            "baseline_total": program.baseline,
            "reform_total": program.reform,
            "change": program.difference,
        }
        for program_name, program in economy.detailed_budget.root.items()
    )


def _district_rows(
    economy: SingleYearMacroOutput,
    keys: Mapping[str, Any],
    district_codes: DistrictCodes | None,
) -> tuple[dict[str, Any], ...]:
    if economy.congressional_district_impact is None:
        return ()
    districts = economy.congressional_district_impact.districts
    if districts and district_codes is None:
        msg = "congressional districts need the US states' FIPS codes"
        raise ValueError(msg)
    return tuple(
        _district_row(district, keys, district_codes)
        for district in districts
        if district_codes is not None
    )


def _district_row(
    district: CongressionalDistrictImpactRecord,
    keys: Mapping[str, Any],
    district_codes: DistrictCodes,
) -> dict[str, Any]:
    state, _, number = district.district.partition("-")
    if state not in district_codes.state_fips or not number.isdigit():
        msg = f"congressional district {district.district!r} names no known state"
        raise ValueError(msg)
    state_fips = district_codes.state_fips[state]
    district_number = 0 if state in district_codes.at_large_states else int(number)
    return {
        "id": uuid4(),
        **keys,
        "district_geoid": state_fips * 100 + district_number,
        "state_fips": state_fips,
        "district_number": district_number,
        "average_household_income_change": district.average_household_income_change,
        "relative_household_income_change": district.relative_household_income_change,
        "winner_percentage": district.winner_percentage,
        "loser_percentage": district.loser_percentage,
        "no_change_percentage": district.no_change_percentage,
        "population": district.population,
    }


def _area_rows(
    areas: GeographicImpactOutput | None,
    keys: Mapping[str, Any],
    kind: Literal["constituency", "local_authority"],
) -> tuple[dict[str, Any], ...]:
    if areas is None:
        return ()
    records: list[ConstituencyImpactRecord | LocalAuthorityImpactRecord] = areas.root
    return tuple(
        {
            "id": uuid4(),
            **keys,
            f"{kind}_code": getattr(record, f"{kind}_code"),
            f"{kind}_name": getattr(record, f"{kind}_name"),
            "average_household_income_change": record.average_household_income_change,
            "relative_household_income_change": (
                record.relative_household_income_change
            ),
            "population": record.population,
            **{field: getattr(record, field) for field in DISCLOSURE_FIELDS},
        }
        for record in records
    )


def _budget_rows(
    economy: SingleYearMacroOutput, keys: Mapping[str, Any], country: str
) -> tuple[dict[str, Any], ...]:
    """Return the headline budget figures; state income tax only for the US.

    Today's output reports 0.0 state income tax for a country without it, so a
    reader without that row reports 0.0 again.
    """
    return tuple(
        {
            "id": uuid4(),
            **keys,
            "variable_name": variable,
            "entity": "household",
            column: getattr(economy.budget, figure),
        }
        for figure, variable, column in BUDGET_FIGURES
        if variable != "household_state_income_tax" or country == "us"
    )


def _decile_rows(
    economy: SingleYearMacroOutput, keys: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    rows = _decile_type_rows(economy.decile, None, keys)
    if economy.wealth_decile is not None:
        rows += _decile_type_rows(economy.wealth_decile, WEALTH_DECILE_VARIABLE, keys)
    return rows


def _decile_type_rows(
    deciles: DecileOutput, decile_variable: str | None, keys: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    """Return the change in household net income in each decile.

    Income deciles rank households by that income, so they name no separate
    ``decile_variable``; wealth deciles name the variable that ranks them.
    """
    return tuple(
        {
            "id": uuid4(),
            **keys,
            "income_variable": "household_net_income",
            "decile_variable": decile_variable,
            "entity": "household",
            "decile": int(decile),
            "quantiles": 10,
            "absolute_change": average,
            # .py's rows hold a percentage; today's output, a fraction.
            "relative_change": deciles.relative[decile] * 100,
        }
        for decile, average in deciles.average.items()
    )


def _intra_decile_rows(
    economy: SingleYearMacroOutput, keys: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    rows = _intra_decile_type_rows(economy.intra_decile, "income", keys)
    if economy.intra_wealth_decile is not None:
        rows += _intra_decile_type_rows(economy.intra_wealth_decile, "wealth", keys)
    return rows


def _intra_decile_type_rows(
    shares: IntraDecileOutput, decile_type: str, keys: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    """Return one row per decile, 1 to 10.

    Row 0 is .py's share across the whole population, which the output
    doesn't carry: its ``all`` is the mean of the decile shares, which a
    reader computes again from these rows.
    """
    decile_count = max((len(values) for values in shares.deciles.values()), default=0)
    return tuple(
        {
            "id": uuid4(),
            **keys,
            "decile_type": decile_type,
            "decile": index + 1,
            **{
                column: shares.deciles[label][index]
                for label, column in INTRA_DECILE_COLUMNS.items()
                if index < len(shares.deciles.get(label, ()))
            },
        }
        for index in range(decile_count)
    )


def _poverty_rows(
    economy: SingleYearMacroOutput, run_ids: ReportRunIds, country: str
) -> tuple[dict[str, Any], ...]:
    """Return each simulation run's poverty rates, for everyone and by group.

    As in .py's rows, ``filter_variable`` names the variable that selects a
    group and ``filter_group`` the group; both are null for everyone.
    """
    poverty_types = POVERTY_TYPES[country]
    return tuple(
        {
            "id": uuid4(),
            "simulation_run_id": simulation_run_id,
            "poverty_type": poverty_types[measure],
            "entity": "person",
            "filter_variable": filter_variable,
            "filter_group": filter_group,
            "rate": getattr(rates, side),
        }
        for side, simulation_run_id in _sides(run_ids)
        for measure, filter_variable, filter_group, rates in _poverty_rates(economy)
    )


def _poverty_rates(
    economy: SingleYearMacroOutput,
) -> list[tuple[str, str | None, str | None, BaselineReformValue]]:
    """Return each measure's rates with the variable and group that select them.

    Ages and genders have both measures; the output breaks only poverty, not
    deep poverty, down by race, and only in the US.
    """
    rates: list[tuple[str, str | None, str | None, BaselineReformValue]] = []
    for measure in ("poverty", "deep_poverty"):
        by_age = getattr(economy.poverty, measure)
        by_gender = getattr(economy.poverty_by_gender, measure)
        rates.append((measure, None, None, by_age.all))
        rates += [
            (measure, "age", group, getattr(by_age, group)) for group in AGE_GROUPS
        ]
        rates += [
            (measure, "is_male", group, getattr(by_gender, group))
            for group in GENDER_GROUPS
        ]
    if economy.poverty_by_race is not None:
        by_race = economy.poverty_by_race.poverty
        rates += [
            ("poverty", "race", group, getattr(by_race, group)) for group in RACE_GROUPS
        ]
    return rates


def _inequality_rows(
    economy: SingleYearMacroOutput, run_ids: ReportRunIds, country: str
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": uuid4(),
            "simulation_run_id": simulation_run_id,
            "income_variable": INEQUALITY_INCOME_VARIABLES[country],
            "entity": "household",
            "gini": getattr(economy.inequality.gini, side),
            "top_10_share": getattr(economy.inequality.top_10_pct_share, side),
            "top_1_share": getattr(economy.inequality.top_1_pct_share, side),
        }
        for side, simulation_run_id in _sides(run_ids)
    )


def _sides(run_ids: ReportRunIds) -> tuple[tuple[str, Any], ...]:
    return (
        ("baseline", run_ids.baseline_simulation_run_id),
        ("reform", run_ids.reform_simulation_run_id),
    )
