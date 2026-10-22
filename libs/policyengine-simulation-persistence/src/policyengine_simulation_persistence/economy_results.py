"""Write an economy comparison's result as rows, and rebuild it from them.

``write_economy_comparison`` runs in the transaction that marks a report run's
comparison step succeeded (``RunTransitions.complete_comparison``), so a run
never succeeds without its rows. ``read_economy_comparison`` rebuilds today's
result format, ``SingleYearMacroOutput``, from the rows and from labels the
region catalog supplies: the model and data versions of the run's catalog
version, and the names and map positions of constituencies and local
authorities by code.

A result the tables cannot hold exactly is refused before any write by
``check_storable``: the average and relative changes of each decile map must
name the same deciles in the same order, the decile shares and averages of
each intra-decile breakdown must name the same categories in the same order,
each pair of labour supply decile maps (average, relative) must name the same
deciles in order, and the area lists must hold records of their own kind,
each area once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.macro_output import (
    ConstituencyImpactRecord,
    DecileOutput,
    IntraDecileOutput,
    LaborSupplyDecileOutput,
    LaborSupplyResponseOutput,
    LocalAuthorityImpactRecord,
    SingleYearMacroOutput,
)
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from policyengine_simulation_persistence.result_tables import (
    POVERTY_GROUPS,
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

# The sections a result may leave null. Which of them a stored result holds is
# recorded on its report run, so a reader returns null exactly where the
# writer received null, including for empty lists.
OPTIONAL_SECTIONS = (
    "poverty_by_race",
    "wealth_decile",
    "intra_wealth_decile",
    "labor_supply_response",
    "constituency_impact",
    "local_authority_impact",
    "congressional_district_impact",
    "cliff_impact",
)
SIDES = ("baseline", "reform")


# Where each field of SingleYearMacroOutput is kept. "{key}" stands for a map
# key and "[]" for a list item; per-simulation figures are kept on the
# baseline's or the reform's own simulation run, keyed by the report run and
# the side too. Labels come from the catalog
# when the result is read. A test requires this to name every field.
REFUSED_OTHER_KIND = "refused by check_storable: a record of the other kind"
_GEOGRAPHY_FIELDS = (
    "average_household_income_change",
    "relative_household_income_change",
    "population",
    *UK_AREA_DISCLOSURE_COLUMNS,
)


def _poverty_storage() -> dict[str, str]:
    paths = {}
    for section, measures, groups in (
        ("poverty", ("poverty", "deep_poverty"), POVERTY_GROUPS["age"]),
        ("poverty_by_gender", ("poverty", "deep_poverty"), POVERTY_GROUPS["gender"]),
        ("poverty_by_race", ("poverty",), POVERTY_GROUPS["race"]),
    ):
        for measure in measures:
            for group in groups:
                for side in SIDES:
                    paths[f"{section}.{measure}.{group}.{side}"] = "poverty_rates.rate"
    return paths


def _geography_storage(
    section: str, own: str, other: str, table: str
) -> dict[str, str]:
    paths = {f"{section}[].{name}": f"{table}.{name}" for name in _GEOGRAPHY_FIELDS}
    paths[f"{section}[].{own}_code"] = f"{table}.{own}_code"
    paths[f"{section}[].{own}_name"] = "region catalog: name"
    paths[f"{section}[].x"] = "region catalog: map x"
    paths[f"{section}[].y"] = "region catalog: map y"
    paths[f"{section}[].{other}_code"] = REFUSED_OTHER_KIND
    paths[f"{section}[].{other}_name"] = REFUSED_OTHER_KIND
    return paths


RESULT_FIELD_STORAGE: dict[str, str] = {
    "spm_provenance": "report_runs.spm_provenance",
    "model_version": "catalog version: model version",
    "data_version": "catalog version: data version",
    **{
        f"budget.{name}": f"budget_impacts.{name}"
        for name in (
            "budgetary_impact",
            "tax_revenue_impact",
            "state_tax_revenue_impact",
            "benefit_spending_impact",
            "households",
            "baseline_net_income",
        )
    },
    **{
        f"detailed_budget.{{key}}.{name}": f"program_budget_impacts.{name}"
        for name in ("baseline", "reform", "difference")
    },
    **{
        f"{section}.{statistic}.{{key}}": f"decile_impacts.{statistic}_change"
        for section in ("decile", "wealth_decile")
        for statistic in ("average", "relative")
    },
    **{
        path: "intra_decile_impacts.share"
        for section in ("intra_decile", "intra_wealth_decile")
        for path in (f"{section}.deciles.{{key}}[]", f"{section}.all.{{key}}")
    },
    **{
        f"inequality.{name}.{side}": f"inequality.{name}"
        for name in ("gini", "top_10_pct_share", "top_1_pct_share")
        for side in SIDES
    },
    **_poverty_storage(),
    **{
        f"cliff_impact.{side}.{name}": f"cliff_impacts.{name}"
        for side in SIDES
        for name in ("cliff_gap", "cliff_share")
    },
    **{
        f"labor_supply_response.{path}": f"labor_supply_responses.{column}"
        for path, column in (
            ("substitution_lsr", "substitution_lsr"),
            ("income_lsr", "income_lsr"),
            ("relative_lsr.income", "relative_income_response"),
            ("relative_lsr.substitution", "relative_substitution_response"),
            ("total_change", "total_change"),
            ("revenue_change", "revenue_change"),
            ("hours.baseline", "baseline_hours"),
            ("hours.reform", "reform_hours"),
            ("hours.change", "hours_change"),
            ("hours.income_effect", "hours_income_effect"),
            ("hours.substitution_effect", "hours_substitution_effect"),
        )
    },
    **{
        f"labor_supply_response.decile.{statistic}.{effect}.{{key}}": (
            f"labor_supply_decile_impacts.{statistic}_{effect}_effect"
        )
        for statistic in ("average", "relative")
        for effect in ("income", "substitution")
    },
    **{
        f"congressional_district_impact.districts[].{name}": (
            f"congressional_district_impacts.{name}"
        )
        for name in (
            "district",
            "average_household_income_change",
            "relative_household_income_change",
            "winner_percentage",
            "loser_percentage",
            "no_change_percentage",
            "population",
        )
    },
    **_geography_storage(
        "constituency_impact", "constituency", "local_authority", "constituency_impacts"
    ),
    **_geography_storage(
        "local_authority_impact",
        "local_authority",
        "constituency",
        "local_authority_impacts",
    ),
}


@dataclass(frozen=True)
class GeographyLabel:
    """A constituency's or local authority's name and map position."""

    name: str
    x: int | None
    y: int | None


@dataclass(frozen=True)
class EconomyResultLabels:
    """What a stored result takes from the region catalog when it is read.

    The model and data versions belong to the run's catalog version; the
    geography labels are looked up by code.
    """

    model_version: str
    data_version: str
    constituencies: Mapping[str, GeographyLabel] = field(default_factory=dict)
    local_authorities: Mapping[str, GeographyLabel] = field(default_factory=dict)


# --- Checks before writing ------------------------------------------------


def _check_decile_maps(name: str, decile: DecileOutput | None) -> None:
    if decile is not None and list(decile.average) != list(decile.relative):
        msg = f"{name}: average and relative must name the same deciles in order"
        raise ValueError(msg)


def _check_intra_decile(name: str, intra: IntraDecileOutput | None) -> None:
    # Today's executor gives each category's decile shares (possibly none,
    # when the breakdown failed) and its average under the same categories.
    if intra is not None and list(intra.deciles) != list(intra.all):
        msg = f"{name}: deciles and all must name the same categories in order"
        raise ValueError(msg)


def _check_unique(section: str, codes: list[str]) -> None:
    if len(codes) != len(set(codes)):
        msg = f"{section} repeats an area"
        raise ValueError(msg)


def _check_labor_supply(labor: LaborSupplyResponseOutput | None) -> None:
    """Require each pair of decile maps to name the same deciles in order.

    The average pair may name deciles the relative pair leaves out, such as
    decile -1 for households with negative income.
    """
    if labor is None:
        return
    metrics = labor.decile
    for statistic, metric in (
        ("average", metrics.average),
        ("relative", metrics.relative),
    ):
        if list(metric.income) != list(metric.substitution):
            msg = (
                f"labor_supply_response.decile.{statistic}: income and "
                "substitution must name the same deciles in order"
            )
            raise ValueError(msg)


def check_storable(result: SingleYearMacroOutput) -> SingleYearMacroOutput:
    """Return ``result`` if the tables can hold it exactly; else raise ValueError."""
    _check_decile_maps("decile", result.decile)
    _check_decile_maps("wealth_decile", result.wealth_decile)
    _check_intra_decile("intra_decile", result.intra_decile)
    _check_intra_decile("intra_wealth_decile", result.intra_wealth_decile)
    _check_labor_supply(result.labor_supply_response)
    for section, record_type in (
        ("constituency_impact", ConstituencyImpactRecord),
        ("local_authority_impact", LocalAuthorityImpactRecord),
    ):
        records = getattr(result, section)
        if records is None:
            continue
        if not all(isinstance(record, record_type) for record in records.root):
            msg = f"{section} must hold only {record_type.__name__} records"
            raise ValueError(msg)
        code_field = f"{section.removesuffix('_impact')}_code"
        _check_unique(section, [getattr(record, code_field) for record in records.root])
    if result.congressional_district_impact is not None:
        _check_unique(
            "congressional_district_impact",
            [
                record.district
                for record in result.congressional_district_impact.districts
            ],
        )
    return result


# --- Writing ----------------------------------------------------------------


def _insert(connection: Connection, table: Table, rows: list[dict[str, Any]]) -> None:
    if rows:
        connection.execute(postgres_insert(table), rows)


def _simulation_rows(
    result: SingleYearMacroOutput, side: str, run_id: UUID, report_run_id: UUID
) -> dict[Table, list[dict[str, Any]]]:
    poverty = []
    groups = [
        ("age", result.poverty.poverty, "poverty"),
        ("age", result.poverty.deep_poverty, "deep_poverty"),
        ("gender", result.poverty_by_gender.poverty, "poverty"),
        ("gender", result.poverty_by_gender.deep_poverty, "deep_poverty"),
    ]
    if result.poverty_by_race is not None:
        groups.append(("race", result.poverty_by_race.poverty, "poverty"))
    for dimension, rates, measure in groups:
        poverty.extend(
            {
                "report_run_id": report_run_id,
                "simulation_run_id": run_id,
                "side": side,
                "measure": measure,
                "group_dimension": dimension,
                "group_value": group,
                "rate": getattr(value, side),
            }
            for group, value in rates
        )
    rows: dict[Table, list[dict[str, Any]]] = {
        poverty_rates: poverty,
        inequality: [
            {
                "report_run_id": report_run_id,
                "simulation_run_id": run_id,
                "side": side,
                **{name: getattr(value, side) for name, value in result.inequality},
            }
        ],
    }
    if result.cliff_impact is not None:
        cliff = getattr(result.cliff_impact, side)
        rows[cliff_impacts] = [
            {
                "report_run_id": report_run_id,
                "simulation_run_id": run_id,
                "side": side,
                **cliff.model_dump(),
            }
        ]
    return rows


def _decile_rows(
    report_run_id: UUID, measure: str, decile: DecileOutput | None
) -> list[dict[str, Any]]:
    if decile is None:
        return []
    return [
        {
            "report_run_id": report_run_id,
            "measure": measure,
            "decile": key,
            "position": position,
            "average_change": decile.average[key],
            "relative_change": decile.relative[key],
        }
        for position, key in enumerate(decile.average)
    ]


def _intra_decile_rows(
    report_run_id: UUID, measure: str, intra: IntraDecileOutput | None
) -> list[dict[str, Any]]:
    if intra is None:
        return []
    rows = [
        {
            "report_run_id": report_run_id,
            "measure": measure,
            "outcome": outcome,
            "outcome_position": position,
            "decile": index,
            "share": share,
        }
        for position, (outcome, shares) in enumerate(intra.deciles.items())
        for index, share in enumerate(shares, start=1)
    ]
    rows.extend(
        {
            "report_run_id": report_run_id,
            "measure": measure,
            "outcome": outcome,
            "outcome_position": position,
            "decile": None,
            "share": share,
        }
        for position, (outcome, share) in enumerate(intra.all.items())
    )
    return rows


def _labor_supply_rows(
    report_run_id: UUID, labor: LaborSupplyResponseOutput | None
) -> dict[Table, list[dict[str, Any]]]:
    if labor is None:
        return {}
    decile = labor.decile
    return {
        labor_supply_responses: [
            {
                "report_run_id": report_run_id,
                "substitution_lsr": labor.substitution_lsr,
                "income_lsr": labor.income_lsr,
                "relative_income_response": labor.relative_lsr.income,
                "relative_substitution_response": labor.relative_lsr.substitution,
                "total_change": labor.total_change,
                "revenue_change": labor.revenue_change,
                "baseline_hours": labor.hours.baseline,
                "reform_hours": labor.hours.reform,
                "hours_change": labor.hours.change,
                "hours_income_effect": labor.hours.income_effect,
                "hours_substitution_effect": labor.hours.substitution_effect,
            }
        ],
        labor_supply_decile_impacts: _labor_supply_decile_rows(report_run_id, decile),
    }


def _labor_supply_decile_rows(
    report_run_id: UUID, decile: LaborSupplyDecileOutput
) -> list[dict[str, Any]]:
    """Return one row per decile either pair of maps names, each with its order."""
    rows: dict[int, dict[str, Any]] = {}
    for statistic, metric in (
        ("average", decile.average),
        ("relative", decile.relative),
    ):
        for position, key in enumerate(metric.income):
            row = rows.setdefault(
                key,
                {
                    "report_run_id": report_run_id,
                    "decile": key,
                    "average_position": None,
                    "relative_position": None,
                    "average_income_effect": None,
                    "average_substitution_effect": None,
                    "relative_income_effect": None,
                    "relative_substitution_effect": None,
                },
            )
            row[f"{statistic}_position"] = position
            row[f"{statistic}_income_effect"] = metric.income[key]
            row[f"{statistic}_substitution_effect"] = metric.substitution[key]
    return list(rows.values())


def _geography_rows(
    report_run_id: UUID, result: SingleYearMacroOutput
) -> dict[Table, list[dict[str, Any]]]:
    rows: dict[Table, list[dict[str, Any]]] = {}
    if result.congressional_district_impact is not None:
        rows[congressional_district_impacts] = [
            {
                "report_run_id": report_run_id,
                "position": position,
                **record.model_dump(),
            }
            for position, record in enumerate(
                result.congressional_district_impact.districts
            )
        ]
    for section, table, code_field in (
        ("constituency_impact", constituency_impacts, "constituency_code"),
        ("local_authority_impact", local_authority_impacts, "local_authority_code"),
    ):
        records = getattr(result, section)
        if records is None:
            continue
        rows[table] = [
            {
                "report_run_id": report_run_id,
                "position": position,
                code_field: getattr(record, code_field),
                "average_household_income_change": (
                    record.average_household_income_change
                ),
                "relative_household_income_change": (
                    record.relative_household_income_change
                ),
                "population": record.population,
                **{name: getattr(record, name) for name in UK_AREA_DISCLOSURE_COLUMNS},
            }
            for position, record in enumerate(records.root)
        ]
    return rows


def _comparison_rows(
    report_run_id: UUID, result: SingleYearMacroOutput
) -> dict[Table, list[dict[str, Any]]]:
    return {
        budget_impacts: [
            {"report_run_id": report_run_id, **result.budget.model_dump()}
        ],
        program_budget_impacts: [
            {
                "report_run_id": report_run_id,
                "program": program,
                "position": position,
                **totals.model_dump(),
            }
            for position, (program, totals) in enumerate(
                result.detailed_budget.root.items()
            )
        ],
        decile_impacts: [
            *_decile_rows(report_run_id, "income", result.decile),
            *_decile_rows(report_run_id, "wealth", result.wealth_decile),
        ],
        intra_decile_impacts: [
            *_intra_decile_rows(report_run_id, "income", result.intra_decile),
            *_intra_decile_rows(report_run_id, "wealth", result.intra_wealth_decile),
        ],
        **_labor_supply_rows(report_run_id, result.labor_supply_response),
        **_geography_rows(report_run_id, result),
    }


def write_economy_comparison(
    connection: Connection,
    report_run: ReportRunRecord,
    result: SingleYearMacroOutput,
) -> None:
    """Write an economy report run's result rows in the caller's transaction.

    The figures of each simulation go to its simulation run, keyed by this
    report run too; the comparison goes to the report run, and the SPM
    provenance and the optional sections the result holds to the report run's
    own columns.
    """
    check_storable(result)
    if report_run.reform_simulation_run_id is None:
        msg = "an economy comparison needs a reform simulation run"
        raise ValueError(msg)
    run_ids = (
        report_run.baseline_simulation_run_id,
        report_run.reform_simulation_run_id,
    )
    for side, run_id in zip(SIDES, run_ids, strict=True):
        for table, rows in _simulation_rows(
            result, side, run_id, report_run.id
        ).items():
            _insert(connection, table, rows)
    for table, rows in _comparison_rows(report_run.id, result).items():
        _insert(connection, table, rows)
    connection.execute(
        update(report_runs)
        .where(report_runs.c.id == report_run.id)
        .values(
            result_sections=[
                section
                for section in OPTIONAL_SECTIONS
                if getattr(result, section) is not None
            ],
            spm_provenance=(
                None
                if result.spm_provenance is None
                else result.spm_provenance.model_dump(mode="json")
            ),
        )
    )


@dataclass(frozen=True)
class EconomyComparisonRecorder:
    """Validate a comparison's output and write it, for the recording loop."""

    def validate(self, output: object) -> SingleYearMacroOutput:
        """Return the output as a result the tables can hold; else raise ValueError."""
        return check_storable(SingleYearMacroOutput.model_validate(output))

    def write(
        self, connection: Connection, report_run: ReportRunRecord, result: object
    ) -> None:
        """Write the validated result's rows in the caller's transaction."""
        if not isinstance(result, SingleYearMacroOutput):
            msg = "the comparison recorder writes only validated economy results"
            raise TypeError(msg)
        write_economy_comparison(connection, report_run, result)


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
            "model_version": labels.model_version,
            "data_version": labels.data_version,
            **_comparison_sections(connection, report_run_id, sections, labels),
            **_simulation_sections(connection, report_run_id, run_ids, sections),
        }
    )
