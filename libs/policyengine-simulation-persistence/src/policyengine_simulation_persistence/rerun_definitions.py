"""What a stored report or simulation calculates, as a re-run reads it.

A re-run (``economy_rerun_records``) first reads the stored record's kind,
country, year, region code, dataset name and each simulation's policy and
dynamic, so the current catalog version's rows with the same code and name
can be found, and, for a budget window, its newest calculation's years.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from policyengine_simulation_persistence.economy_run_records import (
    ECONOMY_REPORT_TYPE,
    SimulationChoice,
)
from policyengine_simulation_persistence.household_report_records import (
    HOUSEHOLD_REPORT_TYPE,
)
from policyengine_simulation_persistence.input_tables import (
    MODEL_NAME_PREFIX,
    datasets,
    regions,
    tax_benefit_model_versions,
    tax_benefit_models,
)
from policyengine_simulation_persistence.tables import (
    SimulationType,
    budget_window_runs,
    report_runs,
    reports,
    simulation_runs,
    simulations,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Connection, RowMapping

# What a stored report or simulation calculates, as a re-run tells them apart:
# a single-year economy report or simulation, a budget window, a household
# report or simulation, or anything else, such as a simulation of a filtered
# population.
type RecordKind = Literal["economy", "budget_window", "household", "unsupported"]


@dataclass(frozen=True)
class SideDefinition:
    """A report side's stored simulation, and its policy and dynamic."""

    simulation_id: UUID
    choice: SimulationChoice


@dataclass(frozen=True)
class WindowYearDefinition:
    """One year of a budget window's newest calculation, and its sides' simulations.

    They are the simulations of the runs that year's report run names: a
    year that reused a run names that run's simulation, which may be another
    report's.
    """

    year: int
    baseline_simulation_id: UUID
    reform_simulation_id: UUID


@dataclass(frozen=True)
class ReportDefinition:
    """What a stored report calculates, as a re-run reads it.

    ``region_code`` and ``dataset_name`` are the code and name of the
    report's region and dataset, or None when the report names none (or a
    row it named was deleted). ``baseline`` and ``reform`` are None when the
    report names no such simulation; a budget window's are its first
    year's. A budget window's ``window_years`` are the years of its newest
    budget-window run whose report run names both sides, earliest first,
    empty when it has none. ``household_id`` is a household report's stored
    household, None for any other report.
    """

    id: UUID
    country: str
    kind: RecordKind
    year: int | None
    region_code: str | None
    dataset_name: str | None
    inputs: dict[str, Any]
    baseline: SideDefinition | None
    reform: SideDefinition | None
    budget_window_size: int | None = None
    window_years: tuple[WindowYearDefinition, ...] = ()
    household_id: UUID | None = None
    # Whether the report was created by key (C1 · 27): its simulations' runs
    # are then shared, whatever report first calculated them.
    keyed: bool = False


@dataclass(frozen=True)
class SimulationDefinition:
    """What a stored simulation calculates, as a re-run reads it.

    ``country`` comes from the simulation's catalog version's model; it is
    None for a model the service does not name ``policyengine-<country>``.
    """

    id: UUID
    country: str | None
    kind: RecordKind
    year: int | None
    region_code: str | None
    dataset_name: str | None
    choice: SimulationChoice


def read_report_definition(
    connection: Connection, report_id: UUID
) -> ReportDefinition | None:
    """Return what the stored report calculates, or None when there is none."""
    region = regions.alias("report_region")
    dataset = datasets.alias("report_dataset")
    baseline = simulations.alias("baseline_simulation")
    reform = simulations.alias("reform_simulation")
    row = (
        connection.execute(
            select(
                reports.c.id,
                reports.c.country,
                reports.c.type,
                reports.c.household_id,
                reports.c.budget_window_size,
                reports.c.year,
                reports.c.inputs,
                reports.c.key_version,
                region.c.code.label("region_code"),
                dataset.c.name.label("dataset_name"),
                baseline.c.id.label("baseline_id"),
                baseline.c.policy_id.label("baseline_policy_id"),
                baseline.c.dynamic_id.label("baseline_dynamic_id"),
                reform.c.id.label("reform_id"),
                reform.c.policy_id.label("reform_policy_id"),
                reform.c.dynamic_id.label("reform_dynamic_id"),
            )
            .select_from(reports)
            .outerjoin(region, region.c.id == reports.c.region_id)
            .outerjoin(dataset, dataset.c.id == reports.c.dataset_id)
            .outerjoin(baseline, baseline.c.id == reports.c.baseline_simulation_id)
            .outerjoin(reform, reform.c.id == reports.c.reform_simulation_id)
            .where(reports.c.id == report_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    kind = _report_kind(row["type"], row["household_id"], row["budget_window_size"])
    return ReportDefinition(
        id=row["id"],
        country=row["country"],
        kind=kind,
        year=row["year"],
        region_code=row["region_code"],
        dataset_name=row["dataset_name"],
        inputs=row["inputs"],
        baseline=_side(row, "baseline"),
        reform=_side(row, "reform"),
        budget_window_size=row["budget_window_size"],
        window_years=(
            _window_years(connection, report_id) if kind == "budget_window" else ()
        ),
        household_id=row["household_id"],
        keyed=row["key_version"] is not None,
    )


def _window_years(
    connection: Connection, report_id: UUID
) -> tuple[WindowYearDefinition, ...]:
    """Return the years of the report's newest budget-window run, earliest first.

    A year whose report run names no reform is left out.
    """
    newest_window_run = (
        select(budget_window_runs.c.id)
        .where(budget_window_runs.c.report_id == report_id)
        .order_by(
            budget_window_runs.c.created_at.desc(), budget_window_runs.c.id.desc()
        )
        .limit(1)
        .scalar_subquery()
    )
    baseline_run = simulation_runs.alias("baseline_run")
    reform_run = simulation_runs.alias("reform_run")
    rows = connection.execute(
        select(
            report_runs.c.year,
            baseline_run.c.simulation_id.label("baseline_simulation_id"),
            reform_run.c.simulation_id.label("reform_simulation_id"),
        )
        .join(
            baseline_run, baseline_run.c.id == report_runs.c.baseline_simulation_run_id
        )
        .join(reform_run, reform_run.c.id == report_runs.c.reform_simulation_run_id)
        .where(report_runs.c.budget_window_run_id == newest_window_run)
        .order_by(report_runs.c.year)
    )
    return tuple(
        WindowYearDefinition(
            year=row.year,
            baseline_simulation_id=row.baseline_simulation_id,
            reform_simulation_id=row.reform_simulation_id,
        )
        for row in rows
    )


def _report_kind(
    report_type: str | None, household_id: UUID | None, budget_window_size: int | None
) -> RecordKind:
    if household_id is not None or report_type == HOUSEHOLD_REPORT_TYPE:
        return "household"
    if report_type != ECONOMY_REPORT_TYPE:
        return "unsupported"
    return "economy" if budget_window_size is None else "budget_window"


def _side(
    row: RowMapping, side: Literal["baseline", "reform"]
) -> SideDefinition | None:
    simulation_id = row[f"{side}_id"]
    if simulation_id is None:
        return None
    return SideDefinition(
        simulation_id=simulation_id,
        choice=SimulationChoice(
            policy_id=row[f"{side}_policy_id"], dynamic_id=row[f"{side}_dynamic_id"]
        ),
    )


def read_simulation_definition(
    connection: Connection, simulation_id: UUID
) -> SimulationDefinition | None:
    """Return what the stored simulation calculates, or None when there is none."""
    row = (
        connection.execute(
            select(
                simulations.c.id,
                simulations.c.simulation_type,
                simulations.c.filter_field,
                simulations.c.year,
                simulations.c.policy_id,
                simulations.c.dynamic_id,
                tax_benefit_models.c.name.label("model_name"),
                regions.c.code.label("region_code"),
                datasets.c.name.label("dataset_name"),
            )
            .select_from(simulations)
            .join(
                tax_benefit_model_versions,
                tax_benefit_model_versions.c.id
                == simulations.c.tax_benefit_model_version_id,
            )
            .join(
                tax_benefit_models,
                tax_benefit_models.c.id == tax_benefit_model_versions.c.model_id,
            )
            .outerjoin(regions, regions.c.id == simulations.c.region_id)
            .outerjoin(datasets, datasets.c.id == simulations.c.dataset_id)
            .where(simulations.c.id == simulation_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    model_name: str = row["model_name"]
    simulation_type = SimulationType(row["simulation_type"])
    kind: RecordKind = (
        "household"
        if simulation_type is SimulationType.HOUSEHOLD
        else "economy"
        if row["filter_field"] is None
        else "unsupported"
    )
    return SimulationDefinition(
        id=row["id"],
        country=model_name.removeprefix(MODEL_NAME_PREFIX)
        if model_name.startswith(MODEL_NAME_PREFIX)
        else None,
        kind=kind,
        year=row["year"],
        region_code=row["region_code"],
        dataset_name=row["dataset_name"],
        choice=SimulationChoice(
            policy_id=row["policy_id"], dynamic_id=row["dynamic_id"]
        ),
    )
