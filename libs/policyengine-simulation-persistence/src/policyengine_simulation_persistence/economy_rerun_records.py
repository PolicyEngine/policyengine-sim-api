"""The simulation service's reads and writes for re-running a report or simulation.

A re-run calculates a stored report or simulation again on the versions the
service runs now, as a new run, and keeps every earlier run. The service:

- reads what the record calculates (``read_report_definition``,
  ``read_simulation_definition``): its country, year, region code, dataset
  name and each simulation's policy and dynamic, so the current catalog
  version's rows with the same code and name can be found;
- creates, in one transaction, the new run and, for each report side not
  reusing a run of its own simulation, a new ``simulation_runs`` row of that
  simulation, all ``pending`` with no Modal call ID. A report's new run has
  trigger ``manual``.

The record itself is never changed: the service may not update ``reports``
or ``simulations``, so a report keeps naming the simulations it was created
with, and each new run records the catalog version, dataset and region it
calculates on. Nothing is created while the record has a run that is
``pending`` or ``running``; a transaction-level advisory lock on the
record's ID makes concurrent re-runs of one record wait for each other, as
a row lock on ``reports`` would need ``UPDATE`` on it.

The service then starts and records the new run's call as it does a new
record's (``economy_run_records``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from sqlalchemy import func, insert, literal, select

from policyengine_simulation_persistence.economy_run_records import (
    ECONOMY_REPORT_TYPE,
    SimulationChoice,
    insert_simulation_run,
    references_checked,
)
from policyengine_simulation_persistence.input_tables import (
    MODEL_NAME_PREFIX,
    datasets,
    regions,
    tax_benefit_model_versions,
    tax_benefit_models,
)
from policyengine_simulation_persistence.tables import (
    ReportRunStatus,
    ReportRunTrigger,
    SimulationStatus,
    SimulationType,
    report_runs,
    reports,
    simulation_runs,
    simulations,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import ColumnElement, Connection, RowMapping, Table

    from policyengine_simulation_persistence.economy_run_records import (
        EconomyPopulation,
        RunVersions,
    )

# What a stored report or simulation calculates, as a re-run tells them apart:
# a single-year economy report or simulation, a budget window, a household
# report or simulation, or anything else, such as a simulation of a filtered
# population.
type RecordKind = Literal["economy", "budget_window", "household", "unsupported"]
# A household report's type, as the household reports' service writes it.
_HOUSEHOLD_TYPE: Final = "household"
# Both run tables' statuses use these values for a run under way.
_UNDER_WAY: Final = (SimulationStatus.PENDING.value, SimulationStatus.RUNNING.value)


@dataclass(frozen=True)
class SideDefinition:
    """A report side's stored simulation, and its policy and dynamic."""

    simulation_id: UUID
    choice: SimulationChoice


@dataclass(frozen=True)
class ReportDefinition:
    """What a stored report calculates, as a re-run reads it.

    ``region_code`` and ``dataset_name`` are the code and name of the
    report's region and dataset, or None when the report names none (or a
    row it named was deleted). ``baseline`` and ``reform`` are None when the
    report names no such simulation.
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


@dataclass(frozen=True)
class RerunSide:
    """The simulation run one side of a report's re-run names.

    The simulation is always the report's own. With ``reused``, the run is
    one of its runs that succeeded on the current versions, and nothing is
    created for the side; otherwise the run is created, pending.
    """

    simulation_id: UUID
    simulation_run_id: UUID
    reused: bool


@dataclass(frozen=True)
class NewReportRerun:
    """A stored report's new run, on the current versions and catalog rows."""

    report_id: UUID
    run_id: UUID
    population: EconomyPopulation
    versions: RunVersions
    baseline: RerunSide
    reform: RerunSide

    @property
    def new_simulation_run_ids(self) -> tuple[UUID, ...]:
        """Return the IDs of the simulation runs created for the re-run."""
        return tuple(
            side.simulation_run_id
            for side in (self.baseline, self.reform)
            if not side.reused
        )


@dataclass(frozen=True)
class NewSimulationRerun:
    """A stored simulation's new run, on the current versions and catalog rows."""

    simulation_id: UUID
    run_id: UUID
    population: EconomyPopulation
    versions: RunVersions


class RunInProgressError(Exception):
    """The record has a run that is pending or running, so no re-run is created.

    ``run_id`` names that run: the newest such one.
    """

    def __init__(self, run_id: UUID) -> None:
        """Keep the run under way."""
        super().__init__(f"run {run_id} is pending or running")
        self.run_id = run_id


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
    return ReportDefinition(
        id=row["id"],
        country=row["country"],
        kind=_report_kind(row["type"], row["household_id"], row["budget_window_size"]),
        year=row["year"],
        region_code=row["region_code"],
        dataset_name=row["dataset_name"],
        inputs=row["inputs"],
        baseline=_side(row, "baseline"),
        reform=_side(row, "reform"),
    )


def _report_kind(
    report_type: str | None, household_id: UUID | None, budget_window_size: int | None
) -> RecordKind:
    if household_id is not None or report_type == _HOUSEHOLD_TYPE:
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


def lock_reruns_of(connection: Connection, record_id: UUID) -> None:
    """Wait for, then hold until the transaction ends, the record's re-run lock.

    It is a transaction-level advisory lock keyed on the record's ID, which
    any role may take: a second re-run of the same report or simulation
    waits here until the first one's transaction ends, then sees its run.
    """
    connection.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(literal(f"rerun:{record_id}"), 0)
            )
        )
    )


def _run_under_way(
    connection: Connection, table: Table, condition: ColumnElement[bool]
) -> UUID | None:
    """Return the newest pending or running run meeting ``condition``, or None."""
    return connection.execute(
        select(table.c.id)
        .where(condition, table.c.status.in_(_UNDER_WAY))
        .order_by(table.c.created_at.desc(), table.c.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def insert_report_rerun(connection: Connection, rerun: NewReportRerun) -> None:
    """Create the report's new run and its sides' new simulation runs, pending.

    Take the report's re-run lock first. Raise RunInProgressError, creating
    nothing, when the report has a run pending or running, and
    EconomyRecordReferenceError when the database refuses a row the new
    runs name; that refusal ends the caller's transaction, or its savepoint.
    """
    lock_reruns_of(connection, rerun.report_id)
    under_way = _run_under_way(
        connection, report_runs, report_runs.c.report_id == rerun.report_id
    )
    if under_way is not None:
        raise RunInProgressError(under_way)
    with references_checked():
        for side in (rerun.baseline, rerun.reform):
            if not side.reused:
                insert_simulation_run(
                    connection,
                    run_id=side.simulation_run_id,
                    simulation_id=side.simulation_id,
                    population=rerun.population,
                    versions=rerun.versions,
                )
        connection.execute(
            insert(report_runs).values(
                id=rerun.run_id,
                report_id=rerun.report_id,
                baseline_simulation_run_id=rerun.baseline.simulation_run_id,
                reform_simulation_run_id=rerun.reform.simulation_run_id,
                budget_window_run_id=None,
                year=None,
                country_package_version=rerun.versions.country_package_version,
                policyengine_version=rerun.versions.policyengine_version,
                status=ReportRunStatus.PENDING,
                trigger=ReportRunTrigger.MANUAL,
                modal_call_id=None,
            )
        )


def insert_simulation_rerun(connection: Connection, rerun: NewSimulationRerun) -> None:
    """Create the simulation's new run, pending with no call.

    Take the simulation's re-run lock first. Raise RunInProgressError,
    creating nothing, when the simulation has a run pending or running, and
    EconomyRecordReferenceError as ``insert_report_rerun`` does.
    """
    lock_reruns_of(connection, rerun.simulation_id)
    under_way = _run_under_way(
        connection,
        simulation_runs,
        simulation_runs.c.simulation_id == rerun.simulation_id,
    )
    if under_way is not None:
        raise RunInProgressError(under_way)
    with references_checked():
        insert_simulation_run(
            connection,
            run_id=rerun.run_id,
            simulation_id=rerun.simulation_id,
            population=rerun.population,
            versions=rerun.versions,
        )
