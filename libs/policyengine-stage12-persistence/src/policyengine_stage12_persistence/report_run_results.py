"""A report run's economy output as rows of the Public API's result tables.

The output is the single-year economy result Stage 12's coordinator builds,
in the format API v1 and v3 read today. Each part with a lossless place in a
result table becomes rows: per-report parts keyed by the report run and its
two simulation runs, poverty and inequality keyed by each simulation run. The
labour supply response and cliff impact are stored whole on the report run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from sqlalchemy import Column, Float, Integer, String, Table
from sqlalchemy.dialects.postgresql import ENUM, UUID

from policyengine_stage12_persistence.report_run_tables import metadata

if TYPE_CHECKING:
    from collections.abc import Mapping

    from policyengine_simulation_contract.macro_output import IntraDecileOutput
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
# The intra-decile row holding each category's share across all deciles.
ALL_DECILES = 0


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
    labor_supply_result: dict[str, Any] | None
    cliff_impact_result: dict[str, Any] | None


def report_run_result(
    output: Mapping[str, Any],
    *,
    run_ids: ReportRunIds,
    country: str,
) -> ReportRunResult:
    """Validate a single-year economy output and return the rows it is stored as.

    Raises ValueError for an output that is not a single-year economy result
    or a country without poverty types.
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
    return ReportRunResult(
        rows=(
            ResultRows(
                budget_summary,
                _budget_rows(economy, report_keys),
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
        ),
        labor_supply_result=dumped["labor_supply_response"],
        cliff_impact_result=dumped["cliff_impact"],
    )


def _budget_rows(
    economy: SingleYearMacroOutput, keys: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": uuid4(),
            **keys,
            "variable_name": variable,
            "entity": "household",
            column: getattr(economy.budget, figure),
        }
        for figure, variable, column in BUDGET_FIGURES
    )


def _decile_rows(
    economy: SingleYearMacroOutput, keys: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": uuid4(),
            **keys,
            "income_variable": "household_net_income",
            "entity": "household",
            "decile": int(decile),
            "quantiles": 10,
            "absolute_change": average,
            "relative_change": economy.decile.relative[decile],
        }
        for decile, average in economy.decile.average.items()
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
    decile_count = max((len(values) for values in shares.deciles.values()), default=0)
    by_decile: list[dict[str, Any]] = [
        {
            column: shares.deciles[label][index]
            for label, column in INTRA_DECILE_COLUMNS.items()
            if index < len(shares.deciles.get(label, ()))
        }
        | {"decile": index + 1}
        for index in range(decile_count)
    ]
    overall = {
        column: shares.all[label]
        for label, column in INTRA_DECILE_COLUMNS.items()
        if label in shares.all
    } | {"decile": ALL_DECILES}
    return tuple(
        {"id": uuid4(), **keys, "decile_type": decile_type, **values}
        for values in (*by_decile, overall)
    )


def _poverty_rows(
    economy: SingleYearMacroOutput, run_ids: ReportRunIds, country: str
) -> tuple[dict[str, Any], ...]:
    poverty_types = POVERTY_TYPES[country]
    blocks: list[tuple[str, Mapping[str, Any]]] = [
        ("poverty", economy.poverty.poverty.model_dump()),
        ("deep_poverty", economy.poverty.deep_poverty.model_dump()),
        ("poverty", economy.poverty_by_gender.poverty.model_dump()),
        ("deep_poverty", economy.poverty_by_gender.deep_poverty.model_dump()),
    ]
    if economy.poverty_by_race is not None:
        blocks.append(("poverty", economy.poverty_by_race.poverty.model_dump()))
    return tuple(
        {
            "id": uuid4(),
            "simulation_run_id": simulation_run_id,
            "poverty_type": poverty_types[measure],
            "entity": "person",
            # Everyone's rate has no filter; each group's names the group.
            "filter_variable": None if group == "all" else group,
            "rate": rates[side],
        }
        for side, simulation_run_id in _sides(run_ids)
        for measure, groups in blocks
        for group, rates in groups.items()
    )


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
