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
  trigger ``manual``. A budget window's new run is a ``budget_window_runs``
  row with a report run per year, each year on the simulations its report
  run in the report's newest window run names. A household report's new run
  gives each side a new run of its own household simulation: household runs
  are never reused.

The record itself is never changed: the service may not update ``reports``
or ``simulations``, so a report keeps naming the simulations it was created
with, and each new run records the catalog version, dataset and region it
calculates on. Nothing is created while the record has a run that is
``pending`` or ``running`` (for a report, a budget-window run or a report
run of it); a transaction-level advisory lock on the
record's ID makes concurrent re-runs of one record wait for each other, as
a row lock on ``reports`` would need ``UPDATE`` on it.

A forced re-run (C1 · 24's answer 3) is an extra run instead: its report
run, or each year's report run of a budget window, and the simulation runs
it calculates are marked ``forced``, which the Public API's one-unfailed-run
indexes leave out, so it is created beside any run on the same versions,
even one under way, and nothing ever joins it.

The service then starts and records the new run's call as it does a new
record's (``economy_run_records``).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Literal

from sqlalchemy import func, insert, literal, select

from policyengine_simulation_persistence.budget_window_records import (
    check_window_years,
    year_run_row,
)
from policyengine_simulation_persistence.economy_run_records import (
    ECONOMY_REPORT_TYPE,
    EconomyRecordReferenceError,
    SimulationChoice,
    insert_simulation_run,
    references_checked,
)
from policyengine_simulation_persistence.household_report_records import (
    HOUSEHOLD_REPORT_TYPE,
    insert_household_simulation_run,
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
    budget_window_runs,
    report_runs,
    reports,
    simulation_runs,
    simulations,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from sqlalchemy import Column, ColumnElement, Connection, RowMapping, Table

    from policyengine_simulation_persistence.economy_run_records import (
        EconomyPopulation,
        RunVersions,
    )

# What a stored report or simulation calculates, as a re-run tells them apart:
# a single-year economy report or simulation, a budget window, a household
# report or simulation, or anything else, such as a simulation of a filtered
# population.
type RecordKind = Literal["economy", "budget_window", "household", "unsupported"]
# Every run table's statuses (simulation, report and budget-window runs) use
# these values for a run under way.
_UNDER_WAY: Final = (SimulationStatus.PENDING.value, SimulationStatus.RUNNING.value)


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
    """A stored report's new run, on the current versions and catalog rows.

    ``forced`` makes it a forced re-run's extra run, which calculates both
    sides.
    """

    report_id: UUID
    run_id: UUID
    population: EconomyPopulation
    versions: RunVersions
    baseline: RerunSide
    reform: RerunSide
    forced: bool = False

    def __post_init__(self) -> None:
        """Refuse a forced re-run naming a run it doesn't create."""
        if self.forced and (self.baseline.reused or self.reform.reused):
            msg = "a forced re-run calculates both sides, reusing no run"
            raise ValueError(msg)

    @property
    def new_simulation_run_ids(self) -> tuple[UUID, ...]:
        """Return the IDs of the simulation runs created for the re-run."""
        return tuple(
            side.simulation_run_id
            for side in (self.baseline, self.reform)
            if not side.reused
        )


@dataclass(frozen=True)
class WindowRerunYear:
    """One year of a budget window's re-run: its report run and each side's run."""

    year: int
    report_run_id: UUID
    baseline: RerunSide
    reform: RerunSide


@dataclass(frozen=True)
class NewWindowRerun:
    """A stored budget window's new calculation, a report run per year.

    ``population`` is the first year's, on the current catalog version;
    each year calculates on the same catalog version, region and dataset in
    its own year. ``recalculate`` records whether the re-run was forced.
    """

    report_id: UUID
    window_run_id: UUID
    population: EconomyPopulation
    versions: RunVersions
    recalculate: bool
    years: tuple[WindowRerunYear, ...]

    def __post_init__(self) -> None:
        """Refuse years that aren't 1 to 75 consecutive ones from the first."""
        check_window_years(self.population.year, [year.year for year in self.years])

    def population_in(self, year: int) -> EconomyPopulation:
        """Return what the window's simulations of ``year`` calculate on."""
        return replace(self.population, year=year)


@dataclass(frozen=True)
class HouseholdRerunSide:
    """One side of a household report's re-run: a new run of its own simulation."""

    simulation_id: UUID
    simulation_run_id: UUID


@dataclass(frozen=True)
class NewHouseholdReportRerun:
    """A stored household report's new run, on the current catalog version.

    Each side gets a new run of its own household simulation, in the
    report's ``year``; ``reform`` is None for a report of the baseline alone.
    """

    report_id: UUID
    run_id: UUID
    tax_benefit_model_version_id: UUID
    year: int
    versions: RunVersions
    baseline: HouseholdRerunSide
    reform: HouseholdRerunSide | None

    @property
    def sides(self) -> tuple[HouseholdRerunSide, ...]:
        """Return each side the re-run calculates, the baseline's first."""
        return (self.baseline,) if self.reform is None else (self.baseline, self.reform)

    @property
    def simulation_run_ids(self) -> tuple[UUID, ...]:
        """Return the IDs of the simulation runs created for the re-run."""
        return tuple(side.simulation_run_id for side in self.sides)


@dataclass(frozen=True)
class NewSimulationRerun:
    """A stored simulation's new run, on the current versions and catalog rows."""

    simulation_id: UUID
    run_id: UUID
    population: EconomyPopulation
    versions: RunVersions


class RerunRecordDeletedError(LookupError):
    """The re-run's report or simulation was deleted after the re-run read it.

    The database refused the new run's key to the record itself; nothing was
    created.
    """


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

    Unless the re-run is forced, take the report's re-run lock first, and
    raise RunInProgressError, creating nothing, when the report has a run
    pending or running; a forced re-run's runs are marked forced. Raise
    RerunRecordDeletedError when the database refuses the new run's key to
    the report, which no longer exists, and EconomyRecordReferenceError when
    it refuses another row the new runs name, such as a side's simulation;
    either refusal ends the caller's transaction, or its savepoint.
    """
    if not rerun.forced:
        lock_reruns_of(connection, rerun.report_id)
        _refuse_while_report_runs(connection, rerun.report_id)
    with _record_checked(report_runs.c.report_id), references_checked():
        for side in (rerun.baseline, rerun.reform):
            if not side.reused:
                insert_simulation_run(
                    connection,
                    run_id=side.simulation_run_id,
                    simulation_id=side.simulation_id,
                    population=rerun.population,
                    versions=rerun.versions,
                    forced=rerun.forced,
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
                forced=rerun.forced,
            )
        )


def insert_window_rerun(connection: Connection, rerun: NewWindowRerun) -> None:
    """Create the budget window's new run, each year's report run and new runs.

    The budget-window run has trigger ``manual`` and records ``recalculate``;
    each year's report run names it and its year, and each side reusing no
    run gets a new run of its year's simulation. All are pending with no
    Modal call; the service then starts the first years. Raise as
    ``insert_report_rerun`` does. A re-run that recalculates is a forced
    one: no lock or refusal, and its year runs and simulation runs are
    marked forced.
    """
    if not rerun.recalculate:
        lock_reruns_of(connection, rerun.report_id)
        _refuse_while_report_runs(connection, rerun.report_id)
    population = rerun.population
    with (
        _record_checked(budget_window_runs.c.report_id, report_runs.c.report_id),
        references_checked(),
    ):
        # First, so a report deleted after its reads is told apart from a
        # year's simulation deleted with it.
        connection.execute(
            insert(budget_window_runs).values(
                id=rerun.window_run_id,
                report_id=rerun.report_id,
                tax_benefit_model_version_id=population.tax_benefit_model_version_id,
                trigger=ReportRunTrigger.MANUAL,
                recalculate=rerun.recalculate,
                status=ReportRunStatus.PENDING,
            )
        )
        for window_year in rerun.years:
            for side in (window_year.baseline, window_year.reform):
                if not side.reused:
                    insert_simulation_run(
                        connection,
                        run_id=side.simulation_run_id,
                        simulation_id=side.simulation_id,
                        population=rerun.population_in(window_year.year),
                        versions=rerun.versions,
                        forced=rerun.recalculate,
                    )
        connection.execute(
            insert(report_runs),
            [
                year_run_row(
                    window_year,
                    report_id=rerun.report_id,
                    window_run_id=rerun.window_run_id,
                    versions=rerun.versions,
                    trigger=ReportRunTrigger.MANUAL,
                    forced=rerun.recalculate,
                )
                for window_year in rerun.years
            ],
        )


def insert_household_report_rerun(
    connection: Connection, rerun: NewHouseholdReportRerun
) -> None:
    """Create the household report's new run and a new run of each side, pending.

    The report run has trigger ``manual`` and names no budget-window run;
    each side's run is of the report's own household simulation, with no
    Modal call: the service then starts one household function call for the
    report run. Raise as ``insert_report_rerun`` does.
    """
    lock_reruns_of(connection, rerun.report_id)
    _refuse_while_report_runs(connection, rerun.report_id)
    with _record_checked(report_runs.c.report_id), references_checked():
        for side in rerun.sides:
            insert_household_simulation_run(
                connection,
                run_id=side.simulation_run_id,
                simulation_id=side.simulation_id,
                year=rerun.year,
                tax_benefit_model_version_id=rerun.tax_benefit_model_version_id,
                versions=rerun.versions,
            )
        connection.execute(
            insert(report_runs).values(
                id=rerun.run_id,
                report_id=rerun.report_id,
                baseline_simulation_run_id=rerun.baseline.simulation_run_id,
                reform_simulation_run_id=(
                    None if rerun.reform is None else rerun.reform.simulation_run_id
                ),
                budget_window_run_id=None,
                year=None,
                country_package_version=rerun.versions.country_package_version,
                policyengine_version=rerun.versions.policyengine_version,
                status=ReportRunStatus.PENDING,
                trigger=ReportRunTrigger.MANUAL,
                modal_call_id=None,
            )
        )


def _refuse_while_report_runs(connection: Connection, report_id: UUID) -> None:
    """Raise RunInProgressError while the report has a run pending or running.

    A budget-window run under way is named first, then a report run.
    """
    under_way = _run_under_way(
        connection, budget_window_runs, budget_window_runs.c.report_id == report_id
    ) or _run_under_way(connection, report_runs, report_runs.c.report_id == report_id)
    if under_way is not None:
        raise RunInProgressError(under_way)


def insert_simulation_rerun(connection: Connection, rerun: NewSimulationRerun) -> None:
    """Create the simulation's new run, pending with no call.

    Take the simulation's re-run lock first. Raise RunInProgressError,
    creating nothing, when the simulation has a run pending or running, and
    RerunRecordDeletedError or EconomyRecordReferenceError as
    ``insert_report_rerun`` does.
    """
    lock_reruns_of(connection, rerun.simulation_id)
    under_way = _run_under_way(
        connection,
        simulation_runs,
        simulation_runs.c.simulation_id == rerun.simulation_id,
    )
    if under_way is not None:
        raise RunInProgressError(under_way)
    with _record_checked(simulation_runs.c.simulation_id), references_checked():
        insert_simulation_run(
            connection,
            run_id=rerun.run_id,
            simulation_id=rerun.simulation_id,
            population=rerun.population,
            versions=rerun.versions,
        )


@contextmanager
def _record_checked(*own_keys: Column[Any]) -> Iterator[None]:
    """Turn a refusal of a new run's key to the record into RerunRecordDeletedError.

    ``own_keys`` are those keys' columns; a refusal of any other row
    propagates unchanged.
    """
    try:
        yield
    except EconomyRecordReferenceError as error:
        refused = (error.table_name, error.column_name)
        if refused in {(key.table.name, key.key) for key in own_keys}:
            raise RerunRecordDeletedError from None
        raise
