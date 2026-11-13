"""The simulation service's reads and writes for re-running a report or simulation.

A re-run calculates a stored report or simulation again on the versions the
service runs now, as a new run, and keeps every earlier run. The service:

- reads what the record calculates (``read_report_definition``,
  ``read_simulation_definition``): its country, year, region code, dataset
  name and each simulation's policy and dynamic, so the current catalog
  version's rows with the same code and name can be found;
- creates, in one transaction, the new run and each report side's run of
  its own simulation, all ``pending`` with no Modal call ID; an unforced
  economy or household report re-run's side joins its simulation's run on
  the current versions instead, when there is one. A report's new run has
  trigger ``manual``. A budget window's new run is a ``budget_window_runs``
  row with a report run per year, each year on the simulations its report
  run in the report's newest window run names. A household report's new
  run names runs of its own household simulations.

The record itself is never changed: the service may not update ``reports``
or ``simulations``, so a report keeps naming the simulations it was created
with, and each new run records the catalog version, dataset and region it
calculates on. A simulation's re-run, and an unforced economy or household
report's, joins the record's run that hasn't failed and wasn't forced on
the current versions, pending, running or succeeded, rather than create
one (C1 · 24's answers 2, 3 and 8; ``shared_rows``): the simulation's run
on those versions, catalog version and year, or the report's run on those
versions. An unforced report re-run (economy, budget window or household)
takes a transaction-level advisory lock on the report's ID first, which
makes concurrent re-runs of one report wait for each other, as a row lock
on ``reports`` would need ``UPDATE`` on it. An unforced budget window's
re-run joins nothing: it is refused while a run of the report is
``pending`` or ``running`` (a budget-window run or a report run); the
service forces every window re-run, as windows have no keys yet. A
simulation's re-run takes no lock.

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
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import func, insert, literal, select

from policyengine_simulation_persistence.budget_window_records import (
    check_window_years,
    year_run_row,
)
from policyengine_simulation_persistence.economy_run_records import (
    EconomyRecordReferenceError,
    insert_simulation_run,
    references_checked,
    simulation_run_row,
)
from policyengine_simulation_persistence.household_report_records import (
    household_simulation_run_row,
    insert_household_simulation_run,
)
from policyengine_simulation_persistence.keyed_economy_records import (
    find_or_insert_report_run_on_sides,
)
from policyengine_simulation_persistence.rerun_definitions import (
    RecordKind,
    ReportDefinition,
    SideDefinition,
    SimulationDefinition,
    WindowYearDefinition,
    read_report_definition,
    read_simulation_definition,
)
from policyengine_simulation_persistence.shared_rows import (
    FoundReportRun,
    find_joinable_report_run,
    find_or_insert_simulation_run,
)
from policyengine_simulation_persistence.tables import (
    ReportRunStatus,
    ReportRunTrigger,
    SimulationStatus,
    budget_window_runs,
    report_runs,
    simulation_runs,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from uuid import UUID

    from sqlalchemy import Column, ColumnElement, Connection, Table

    from policyengine_simulation_persistence.economy_run_records import (
        EconomyPopulation,
        RunVersions,
    )
    from policyengine_simulation_persistence.keyed_economy_records import Side
    from policyengine_simulation_persistence.shared_rows import FoundSimulationRun

# Every run table's statuses (simulation, report and budget-window runs) use
# these values for a run under way.
_UNDER_WAY: Final = (SimulationStatus.PENDING.value, SimulationStatus.RUNNING.value)


@dataclass(frozen=True)
class RerunSide:
    """The simulation run one side of a report's or a window year's re-run names.

    The simulation is always the report's own. With ``reused`` (an unforced
    budget window's year only), the run is one of its runs that succeeded
    on the current versions, and nothing is created for the side; otherwise
    ``simulation_run_id`` is the run created, pending, or the ID an economy
    report's side proposes, used only if it inserts the run.
    """

    simulation_id: UUID
    simulation_run_id: UUID
    reused: bool


@dataclass(frozen=True)
class NewReportRerun:
    """A stored report's new run, on the current versions and catalog rows.

    Unforced, each side joins its simulation's run on the re-run's versions
    that hasn't failed and wasn't forced, or inserts one; a side's
    ``simulation_run_id`` is the ID it proposes, used only if it inserts the
    run. ``forced`` makes it a forced re-run's extra run, which inserts a
    new run of each side, marked forced. No side names a reused run.
    """

    report_id: UUID
    run_id: UUID
    population: EconomyPopulation
    versions: RunVersions
    baseline: RerunSide
    reform: RerunSide
    forced: bool = False

    def __post_init__(self) -> None:
        """Refuse a side naming a reused run: each side joins or creates its run."""
        if self.baseline.reused or self.reform.reused:
            msg = "a report re-run's sides join or create their runs, reusing none"
            raise ValueError(msg)

    @property
    def new_simulation_run_ids(self) -> tuple[UUID, ...]:
        """Return the IDs of the simulation runs a forced re-run creates.

        An unforced re-run's sides propose these IDs; each is used only for
        a run the re-run inserts.
        """
        return (self.baseline.simulation_run_id, self.reform.simulation_run_id)


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

    Unforced (a keyed report's re-run), each side joins its own household
    simulation's run in the report's ``year`` on the re-run's versions, or
    inserts one with the ID it proposes. ``forced`` makes it an extra run,
    on a new run of each side, all marked forced. ``reform`` is None for a
    report of the baseline alone.
    """

    report_id: UUID
    run_id: UUID
    tax_benefit_model_version_id: UUID
    year: int
    versions: RunVersions
    baseline: HouseholdRerunSide
    reform: HouseholdRerunSide | None
    forced: bool = False

    @property
    def named_sides(self) -> dict[Side, HouseholdRerunSide]:
        """Return each side the re-run calculates, by its name."""
        sides: dict[Side, HouseholdRerunSide] = {"baseline": self.baseline}
        if self.reform is not None:
            sides["reform"] = self.reform
        return sides

    @property
    def sides(self) -> tuple[HouseholdRerunSide, ...]:
        """Return each side the re-run calculates, the baseline's first."""
        return (self.baseline,) if self.reform is None else (self.baseline, self.reform)

    @property
    def simulation_run_ids(self) -> tuple[UUID, ...]:
        """Return the IDs the re-run's sides propose for their simulation runs.

        A forced re-run creates them all; an unforced one only those it
        inserts.
        """
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


def lock_reruns_of(connection: Connection, record_id: UUID) -> None:
    """Wait for, then hold until the transaction ends, the record's re-run lock.

    It is a transaction-level advisory lock keyed on the record's ID, which
    any role may take: a second re-run of the same report waits here until
    the first one's transaction ends, then sees its run, which an economy
    or household report's re-run joins and a budget window's is refused
    for. A simulation's re-run takes none, as the Public API's index on runs
    lets one joinable run exist, and neither does a forced re-run, which
    nothing joins.
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


def insert_report_rerun(
    connection: Connection, rerun: NewReportRerun
) -> FoundReportRun:
    """Join the report's run on the re-run's versions, or create it, pending.

    Unless the re-run is forced, take the report's re-run lock, raise
    RunInProgressError, creating nothing, while a budget-window run of the
    report is pending or running (the service re-runs a budget window with
    ``insert_window_rerun``; one passed here directly is refused that way),
    and join the report's run on the re-run's versions that hasn't failed
    and wasn't forced, when there is one. Otherwise each side joins its
    simulation's run on those versions, or inserts one
    (``find_or_insert_side_runs``), and the report run naming them is
    inserted, unless another request inserted one after the lookup: that
    run is joined, and this call's simulation runs are taken back. A forced
    re-run inserts its runs, marked forced, and is never joined. Return the
    run and whether this call created it, which only then needs its Modal
    call.

    Raise RerunRecordDeletedError when the database refuses the new run's
    key to the report, which no longer exists, and
    EconomyRecordReferenceError when it refuses another row the new runs
    name, such as a side's simulation; either refusal ends the caller's
    transaction, or its savepoint.
    """
    versions = rerun.versions
    if not rerun.forced:
        lock_reruns_of(connection, rerun.report_id)
        _refuse_while_window_runs(connection, rerun.report_id)
        joined = find_joinable_report_run(
            connection,
            report_id=rerun.report_id,
            country_package_version=versions.country_package_version,
            policyengine_version=versions.policyengine_version,
        )
        if joined is not None:
            return joined
    row = _rerun_report_run_row(
        run_id=rerun.run_id, report_id=rerun.report_id, versions=versions
    )
    sides: dict[Side, RerunSide] = {"baseline": rerun.baseline, "reform": rerun.reform}
    with _record_checked(report_runs.c.report_id), references_checked():
        if rerun.forced:
            for side in sides.values():
                insert_simulation_run(
                    connection,
                    run_id=side.simulation_run_id,
                    simulation_id=side.simulation_id,
                    population=rerun.population,
                    versions=versions,
                    forced=True,
                )
            return _insert_forced_report_run(connection, row, sides)
        found, _ = find_or_insert_report_run_on_sides(
            connection,
            row,
            {
                name: simulation_run_row(
                    run_id=side.simulation_run_id,
                    simulation_id=side.simulation_id,
                    population=rerun.population,
                    versions=versions,
                )
                for name, side in sides.items()
            },
        )
        return found


def _rerun_report_run_row(
    *, run_id: UUID, report_id: UUID, versions: RunVersions
) -> dict[str, Any]:
    """Return a re-run's report run as inserted, without its sides' runs.

    It names no trigger: each user's report-run record holds it (answer 6).
    """
    return {
        "id": run_id,
        "report_id": report_id,
        "budget_window_run_id": None,
        "year": None,
        "country_package_version": versions.country_package_version,
        "policyengine_version": versions.policyengine_version,
        "status": ReportRunStatus.PENDING,
        "modal_call_id": None,
    }


def _insert_forced_report_run(
    connection: Connection,
    row: dict[str, Any],
    side_runs: Mapping[Side, RerunSide | HouseholdRerunSide],
) -> FoundReportRun:
    """Insert a forced re-run's report run, naming its sides' new runs."""
    reform = side_runs.get("reform")
    baseline_run_id = side_runs["baseline"].simulation_run_id
    reform_run_id = None if reform is None else reform.simulation_run_id
    connection.execute(
        insert(report_runs).values(
            {
                **row,
                "baseline_simulation_run_id": baseline_run_id,
                "reform_simulation_run_id": reform_run_id,
                "forced": True,
            }
        )
    )
    return FoundReportRun(
        id=row["id"],
        inserted=True,
        status=ReportRunStatus.PENDING,
        baseline_simulation_run_id=baseline_run_id,
        reform_simulation_run_id=reform_run_id,
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
) -> FoundReportRun:
    """Join the household report's run on the re-run's versions, or create it.

    The report run has trigger ``manual`` and names no budget-window run;
    each side's run is of the report's own household simulation, with no
    Modal call: the service then starts one household function call for a
    report run it created. Unforced (a keyed report's re-run), take the
    report's re-run lock and join the report's run on the re-run's versions
    that hasn't failed and wasn't forced, when there is one; otherwise each
    side joins its simulation's run on those versions, or inserts one, under
    a new report run (``find_or_insert_report_run_on_sides``), as an
    economy report's re-run does (C1 · 24's answer 8). A forced re-run, as
    every re-run of a household report without keys is (interim answer B),
    takes no lock and is never refused: its report run and a new run of
    each side are marked forced, which nothing joins and the
    one-unfailed-run indexes leave out. Return the run and whether this call
    created it, which only then needs its call. Raise as
    ``insert_report_rerun`` does.
    """
    versions = rerun.versions
    row = _rerun_report_run_row(
        run_id=rerun.run_id, report_id=rerun.report_id, versions=versions
    )
    sides = rerun.named_sides
    if not rerun.forced:
        lock_reruns_of(connection, rerun.report_id)
        joined = find_joinable_report_run(
            connection,
            report_id=rerun.report_id,
            country_package_version=versions.country_package_version,
            policyengine_version=versions.policyengine_version,
        )
        if joined is not None:
            return joined
    with _record_checked(report_runs.c.report_id), references_checked():
        if rerun.forced:
            for side in sides.values():
                insert_household_simulation_run(
                    connection,
                    run_id=side.simulation_run_id,
                    simulation_id=side.simulation_id,
                    year=rerun.year,
                    tax_benefit_model_version_id=rerun.tax_benefit_model_version_id,
                    versions=versions,
                    forced=True,
                )
            return _insert_forced_report_run(connection, row, sides)
        found, _ = find_or_insert_report_run_on_sides(
            connection,
            row,
            {
                name: household_simulation_run_row(
                    run_id=side.simulation_run_id,
                    simulation_id=side.simulation_id,
                    year=rerun.year,
                    tax_benefit_model_version_id=rerun.tax_benefit_model_version_id,
                    versions=versions,
                )
                for name, side in sides.items()
            },
        )
        return found


def _refuse_while_window_runs(connection: Connection, report_id: UUID) -> None:
    """Raise RunInProgressError while the report has a budget-window run under way."""
    under_way = _run_under_way(
        connection, budget_window_runs, budget_window_runs.c.report_id == report_id
    )
    if under_way is not None:
        raise RunInProgressError(under_way)


def _refuse_while_report_runs(connection: Connection, report_id: UUID) -> None:
    """Raise RunInProgressError while the report has a run pending or running.

    A budget-window run under way is named first, then a report run.
    """
    _refuse_while_window_runs(connection, report_id)
    under_way = _run_under_way(
        connection, report_runs, report_runs.c.report_id == report_id
    )
    if under_way is not None:
        raise RunInProgressError(under_way)


def insert_simulation_rerun(
    connection: Connection, rerun: NewSimulationRerun
) -> FoundSimulationRun:
    """Join the simulation's run on the re-run's versions, or create it, pending.

    The run joined is the simulation's that hasn't failed and wasn't forced,
    on the same year, catalog version, country package and .py versions;
    otherwise ``rerun.run_id`` is created with no call. Return the run and
    whether this call created it, which only then needs its Modal call.
    Raise RerunRecordDeletedError or EconomyRecordReferenceError as
    ``insert_report_rerun`` does.
    """
    with _record_checked(simulation_runs.c.simulation_id), references_checked():
        return find_or_insert_simulation_run(
            connection,
            simulation_run_row(
                run_id=rerun.run_id,
                simulation_id=rerun.simulation_id,
                population=rerun.population,
                versions=rerun.versions,
            ),
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


# The definition reads moved to rerun_definitions; their names stay importable here.
__all__ = [
    "HouseholdRerunSide",
    "NewHouseholdReportRerun",
    "NewReportRerun",
    "NewSimulationRerun",
    "NewWindowRerun",
    "RecordKind",
    "ReportDefinition",
    "RerunRecordDeletedError",
    "RerunSide",
    "RunInProgressError",
    "SideDefinition",
    "SimulationDefinition",
    "WindowRerunYear",
    "WindowYearDefinition",
    "insert_household_report_rerun",
    "insert_report_rerun",
    "insert_simulation_rerun",
    "insert_window_rerun",
    "lock_reruns_of",
    "read_report_definition",
    "read_simulation_definition",
]
