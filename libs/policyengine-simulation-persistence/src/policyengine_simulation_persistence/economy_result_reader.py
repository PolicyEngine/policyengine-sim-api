"""Rebuild an economy comparison's result from its rows.

``read_economy_comparison`` rebuilds today's result format,
``SingleYearMacroOutput``, from the rows ``economy_results`` wrote and from
the labels the region catalog supplies (``EconomyResultLabels``): the names
and map positions of constituencies and local authorities by code. The model
and data versions, the SPM provenance and which optional sections the result
holds come from its report run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.macro_output import (
    SingleYearMacroOutput,
)
from sqlalchemy import select

from policyengine_simulation_persistence.economy_result_storage import SIDES
from policyengine_simulation_persistence.result_tables import (
    UK_AREA_DISCLOSURE_COLUMNS,
    budget_impacts,
    cliff_impacts,
    congressional_district_impacts,
    constituency_impacts,
    decile_impacts,
    inequality,
    intra_decile_impacts,
    labor_supply_decile_impacts,
    labor_supply_responses,
    local_authority_impacts,
    poverty_rates,
    program_budget_impacts,
)
from policyengine_simulation_persistence.runs import (
    ReportRunRecord,
    RunStatus,
    report_runs,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

    from sqlalchemy import Connection, RowMapping, Table

    from policyengine_simulation_persistence.economy_result_storage import (
        EconomyResultLabels,
        GeographyLabel,
    )


# --- Reading ----------------------------------------------------------------


def _rows(
    connection: Connection,
    table: Table,
    key_column: str,
    key: UUID,
    order: Sequence[str] = (),
) -> list[RowMapping]:
    statement = select(table).where(table.c[key_column] == key)
    if order:
        statement = statement.order_by(*(table.c[name] for name in order))
    return list(connection.execute(statement).mappings())


def _one(
    connection: Connection, table: Table, key_column: str, key: UUID
) -> RowMapping:
    rows = _rows(connection, table, key_column, key)
    if len(rows) != 1:
        msg = f"{table.name} has no row for {key}"
        raise LookupError(msg)
    return rows[0]


def _poverty(rows_by_side: dict[str, list[RowMapping]]) -> dict[str, Any]:
    sections: dict[tuple[str, str], dict[str, dict[str, float]]] = {}
    for side, side_rows in rows_by_side.items():
        for row in side_rows:
            group = sections.setdefault((row["group_dimension"], row["measure"]), {})
            group.setdefault(row["group_value"], {})[side] = row["rate"]
    return {
        "poverty": {
            "poverty": sections[("age", "poverty")],
            "deep_poverty": sections[("age", "deep_poverty")],
        },
        "poverty_by_gender": {
            "poverty": sections[("gender", "poverty")],
            "deep_poverty": sections[("gender", "deep_poverty")],
        },
        "poverty_by_race": (
            {"poverty": sections[("race", "poverty")]}
            if ("race", "poverty") in sections
            else None
        ),
    }


def _figures(
    connection: Connection,
    table: Table,
    report_run_id: UUID,
    run_id: UUID,
    side: str,
) -> list[RowMapping]:
    """Return one side's own figures as one report run computed them."""
    return list(
        connection.execute(
            select(table).where(
                table.c.report_run_id == report_run_id,
                table.c.simulation_run_id == run_id,
                table.c.side == side,
            )
        ).mappings()
    )


def _one_figure(
    connection: Connection,
    table: Table,
    report_run_id: UUID,
    run_id: UUID,
    side: str,
) -> RowMapping:
    rows = _figures(connection, table, report_run_id, run_id, side)
    if len(rows) != 1:
        msg = (
            f"{table.name} has no {side} row for run {run_id} "
            f"in report run {report_run_id}"
        )
        raise LookupError(msg)
    return rows[0]


def _simulation_sections(
    connection: Connection,
    report_run_id: UUID,
    run_ids: tuple[UUID, UUID],
    sections: set[str],
) -> dict[str, Any]:
    by_side = {
        side: _figures(connection, poverty_rates, report_run_id, run_id, side)
        for side, run_id in zip(SIDES, run_ids, strict=True)
    }
    payload = _poverty(by_side)
    if "poverty_by_race" not in sections:
        payload["poverty_by_race"] = None
    inequality_rows = [
        _one_figure(connection, inequality, report_run_id, run_id, side)
        for side, run_id in zip(SIDES, run_ids, strict=True)
    ]
    payload["inequality"] = {
        name: {
            side: row[name] for side, row in zip(SIDES, inequality_rows, strict=True)
        }
        for name in ("gini", "top_10_pct_share", "top_1_pct_share")
    }
    payload["cliff_impact"] = None
    if "cliff_impact" in sections:
        cliff_rows = [
            _one_figure(connection, cliff_impacts, report_run_id, run_id, side)
            for side, run_id in zip(SIDES, run_ids, strict=True)
        ]
        payload["cliff_impact"] = {
            side: {name: row[name] for name in ("cliff_gap", "cliff_share")}
            for side, row in zip(SIDES, cliff_rows, strict=True)
        }
    return payload


def _deciles(rows: list[RowMapping], measure: str) -> dict[str, dict[str, float]]:
    measured = [row for row in rows if row["measure"] == measure]
    return {
        "average": {row["decile"]: row["average_change"] for row in measured},
        "relative": {row["decile"]: row["relative_change"] for row in measured},
    }


def _intra_deciles(rows: list[RowMapping], measure: str) -> dict[str, Any]:
    """Rebuild one breakdown; its categories come from their average rows.

    A category whose decile shares are missing (the breakdown failed) gets an
    empty list, as it was written.
    """
    measured = [row for row in rows if row["measure"] == measure]
    averages = sorted(
        (row for row in measured if row["decile"] is None),
        key=lambda row: row["outcome_position"],
    )
    deciles: dict[str, list[float]] = {row["outcome"]: [] for row in averages}
    for row in sorted(
        (row for row in measured if row["decile"] is not None),
        key=lambda row: (row["outcome_position"], row["decile"]),
    ):
        deciles[row["outcome"]].append(row["share"])
    return {
        "deciles": deciles,
        "all": {row["outcome"]: row["share"] for row in averages},
    }


def _labor_supply_decile_maps(
    rows: list[RowMapping], statistic: str
) -> dict[str, dict[int, float]]:
    """Rebuild one pair of labour supply decile maps in its own order."""
    named = sorted(
        (row for row in rows if row[f"{statistic}_position"] is not None),
        key=lambda row: row[f"{statistic}_position"],
    )
    return {
        effect: {row["decile"]: row[f"{statistic}_{effect}_effect"] for row in named}
        for effect in ("income", "substitution")
    }


def _labor_supply(connection: Connection, report_run_id: UUID) -> dict[str, Any]:
    response = _one(connection, labor_supply_responses, "report_run_id", report_run_id)
    deciles = _rows(
        connection, labor_supply_decile_impacts, "report_run_id", report_run_id
    )
    return {
        "substitution_lsr": response["substitution_lsr"],
        "income_lsr": response["income_lsr"],
        "relative_lsr": {
            "income": response["relative_income_response"],
            "substitution": response["relative_substitution_response"],
        },
        "total_change": response["total_change"],
        "revenue_change": response["revenue_change"],
        "decile": {
            statistic: _labor_supply_decile_maps(deciles, statistic)
            for statistic in ("average", "relative")
        },
        "hours": {
            "baseline": response["baseline_hours"],
            "reform": response["reform_hours"],
            "change": response["hours_change"],
            "income_effect": response["hours_income_effect"],
            "substitution_effect": response["hours_substitution_effect"],
        },
    }


def _labelled_geographies(
    rows: list[RowMapping],
    code_field: str,
    name_field: str,
    labels: Mapping[str, GeographyLabel],
) -> list[dict[str, Any]]:
    records = []
    for row in rows:
        code = row[code_field]
        label = labels.get(code)
        if label is None:
            msg = f"the region catalog has no label for {code}"
            raise LookupError(msg)
        records.append(
            {
                code_field: code,
                name_field: label.name,
                "x": label.x,
                "y": label.y,
                "average_household_income_change": row[
                    "average_household_income_change"
                ],
                "relative_household_income_change": row[
                    "relative_household_income_change"
                ],
                "population": row["population"],
                **{name: row[name] for name in UK_AREA_DISCLOSURE_COLUMNS},
            }
        )
    return records


def _geography_sections(
    connection: Connection,
    report_run_id: UUID,
    sections: set[str],
    labels: EconomyResultLabels,
) -> dict[str, Any]:
    def ordered(table: Table) -> list[RowMapping]:
        return _rows(
            connection, table, "report_run_id", report_run_id, order=("position",)
        )

    payload: dict[str, Any] = {
        "congressional_district_impact": None,
        "constituency_impact": None,
        "local_authority_impact": None,
    }
    if "congressional_district_impact" in sections:
        payload["congressional_district_impact"] = {
            "districts": [
                {
                    name: row[name]
                    for name in row.keys()  # noqa: SIM118  # RowMapping keys
                    if name not in {"report_run_id", "position"}
                }
                for row in ordered(congressional_district_impacts)
            ]
        }
    if "constituency_impact" in sections:
        payload["constituency_impact"] = _labelled_geographies(
            ordered(constituency_impacts),
            "constituency_code",
            "constituency_name",
            labels.constituencies,
        )
    if "local_authority_impact" in sections:
        payload["local_authority_impact"] = _labelled_geographies(
            ordered(local_authority_impacts),
            "local_authority_code",
            "local_authority_name",
            labels.local_authorities,
        )
    return payload


def _comparison_sections(
    connection: Connection,
    report_run_id: UUID,
    sections: set[str],
    labels: EconomyResultLabels,
) -> dict[str, Any]:
    budget = _one(connection, budget_impacts, "report_run_id", report_run_id)
    deciles = _rows(
        connection,
        decile_impacts,
        "report_run_id",
        report_run_id,
        order=("measure", "position"),
    )
    intra = _rows(connection, intra_decile_impacts, "report_run_id", report_run_id)
    programs = _rows(
        connection,
        program_budget_impacts,
        "report_run_id",
        report_run_id,
        order=("position",),
    )
    return {
        "budget": {
            name: value for name, value in budget.items() if name != "report_run_id"
        },
        "detailed_budget": {
            row["program"]: {
                "baseline": row["baseline"],
                "reform": row["reform"],
                "difference": row["difference"],
            }
            for row in programs
        },
        "decile": _deciles(deciles, "income"),
        "wealth_decile": (
            _deciles(deciles, "wealth") if "wealth_decile" in sections else None
        ),
        "intra_decile": _intra_deciles(intra, "income"),
        "intra_wealth_decile": (
            _intra_deciles(intra, "wealth")
            if "intra_wealth_decile" in sections
            else None
        ),
        "labor_supply_response": (
            _labor_supply(connection, report_run_id)
            if "labor_supply_response" in sections
            else None
        ),
        **_geography_sections(connection, report_run_id, sections, labels),
    }


def read_economy_comparison(
    connection: Connection, report_run_id: UUID, labels: EconomyResultLabels
) -> SingleYearMacroOutput:
    """Rebuild a succeeded economy report run's result from its rows.

    Raise LookupError when the report run has no succeeded comparison, or
    when the labels lack a constituency or local authority the result names.
    """
    report_run_row = (
        connection.execute(select(report_runs).where(report_runs.c.id == report_run_id))
        .mappings()
        .first()
    )
    if report_run_row is None:
        msg = f"no report run {report_run_id}"
        raise LookupError(msg)
    report_run = ReportRunRecord.from_row(report_run_row)
    if (
        report_run.comparison_status is not RunStatus.SUCCEEDED
        or report_run.reform_simulation_run_id is None
    ):
        msg = f"report run {report_run_id} has no succeeded economy comparison"
        raise LookupError(msg)
    sections: set[str] = set(report_run_row["result_sections"] or ())
    run_ids = (
        report_run.baseline_simulation_run_id,
        report_run.reform_simulation_run_id,
    )
    return SingleYearMacroOutput.model_validate(
        {
            "spm_provenance": report_run_row["spm_provenance"],
            "model_version": report_run_row["result_model_version"],
            "data_version": report_run_row["result_data_version"],
            **_comparison_sections(connection, report_run_id, sections, labels),
            **_simulation_sections(connection, report_run_id, run_ids, sections),
        }
    )
