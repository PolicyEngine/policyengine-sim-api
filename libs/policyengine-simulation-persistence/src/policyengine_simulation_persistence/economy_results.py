"""Write an economy comparison's result as rows, and rebuild it from them.

``write_economy_comparison`` runs in the transaction that marks a report run's
comparison step succeeded (``RunTransitions.complete_comparison``), so a run
never succeeds without its rows. ``read_economy_comparison`` rebuilds today's
result format, ``SingleYearMacroOutput``, from the rows and from the labels
the region catalog supplies: the names and map positions of constituencies and
local authorities by code. The model and data versions are stored as the
result reports them, since a run on a non-default dataset (a US state run on
the ACS-local dataset) reports that dataset's revision.

A result the tables cannot hold exactly is refused before any write by
``check_storable``: the average and relative changes of each decile map must
name the same deciles in the same order, the decile shares and averages of
each intra-decile breakdown must name the same categories in the same order,
within each labour supply decile map pair (average, relative) the income and
substitution effects must name the same deciles in order (the average pair may
name deciles the relative pair leaves out, such as decile -1), and the area
lists must hold records of their own kind, each area once.
"""

from __future__ import annotations

from dataclasses import dataclass
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
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from policyengine_simulation_persistence.economy_result_reader import (
    read_economy_comparison,
)
from policyengine_simulation_persistence.economy_result_storage import (
    OPTIONAL_SECTIONS,
    REFUSED_OTHER_KIND,
    RESULT_FIELD_STORAGE,
    SIDES,
    EconomyResultLabels,
    GeographyLabel,
)
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
    report_runs,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Connection, Table

__all__ = [
    "OPTIONAL_SECTIONS",
    "REFUSED_OTHER_KIND",
    "RESULT_FIELD_STORAGE",
    "SIDES",
    "EconomyComparisonRecorder",
    "EconomyResultLabels",
    "GeographyLabel",
    "check_storable",
    "read_economy_comparison",
    "write_economy_comparison",
]

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
    """Require each pair's income and substitution maps to name one decile order.

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
            result_model_version=result.model_version,
            result_data_version=result.data_version,
        )
    )


@dataclass(frozen=True)
class EconomyComparisonRecorder:
    """Validate a comparison's output and write it, as RunRecorder expects."""

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
