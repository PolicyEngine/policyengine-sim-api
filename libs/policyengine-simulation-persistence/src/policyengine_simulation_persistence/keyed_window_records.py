"""Find a budget window by key, with its window run, or create them (answer 8).

A budget window compares the same baseline and reform in each year of a
window of 1 to 75 consecutive years. Identical window requests, together or
apart, end on the same report, simulations, window run and year runs, as
economy reports do (``keyed_economy_records``), and only the request that
inserted the window run starts its first years. A window's create runs, in
one transaction:

1. each year's simulations by their keys (``definition_keys``, each of its
   own year), in key order across every year; two sides with one key name
   one simulation;
2. the report by its key, which holds the window's first year and size,
   naming the first year's simulations;
3. the report's window run on the current catalog version and .py version
   that hasn't failed and isn't a recalculation: when there is one, the
   request joins it, with its year runs, and inserts nothing more;
4. otherwise the window run, recording its .py version, against the Public
   API's window run index (``find_or_insert_window_run``), so a request
   inserting one another request has inserted waits for it and joins it;
   then each distinct simulation's run on those versions, catalog version
   and year that hasn't failed and wasn't forced, found or inserted in
   simulation ID order, and each year's report run naming them.

A year's simulation run may be a single-year report's, or another window's,
pending, running or succeeded (answers 4 and 5). Rows keep random IDs: the
IDs a request proposes are used only for the rows it inserts. No per-user
column is written (answer 6).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import insert, select

from policyengine_simulation_persistence.budget_window_records import (
    check_window_years,
)
from policyengine_simulation_persistence.economy_run_records import (
    insert_simulation_run,
    references_checked,
    simulation_run_row,
)
from policyengine_simulation_persistence.keyed_economy_records import (
    FoundSide,
    ProposedSide,
    Side,
    economy_report_row,
    economy_simulation_row,
)
from policyengine_simulation_persistence.shared_rows import (
    FoundWindowRun,
    find_joinable_window_run,
    find_or_insert_report,
    find_or_insert_simulation,
    find_or_insert_simulation_runs,
    find_or_insert_window_run,
)
from policyengine_simulation_persistence.tables import (
    ReportRunStatus,
    report_runs,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from sqlalchemy import Connection

    from policyengine_simulation_persistence.definition_keys import (
        EconomyReportDefinition,
    )
    from policyengine_simulation_persistence.economy_run_records import (
        EconomyPopulation,
        RunVersions,
    )

# A window year's side: its year and the side's name.
type YearSide = tuple[int, Side]
_SIDE_NAMES: tuple[Side, Side] = ("baseline", "reform")


@dataclass(frozen=True)
class WindowYearToInsert:
    """A window year to insert: its report run and each side's simulation.

    ``proposed_run_ids`` are the IDs each side's run gets if it is inserted.
    """

    year: int
    report_run_id: UUID
    simulation_ids: dict[Side, UUID]
    proposed_run_ids: dict[Side, UUID]


@dataclass(frozen=True)
class KeyedWindowYear:
    """A year a window request names: the IDs its rows get if it inserts them."""

    year: int
    report_run_id: UUID
    baseline: ProposedSide
    reform: ProposedSide


@dataclass(frozen=True)
class KeyedBudgetWindow:
    """A budget window a request names, found by key or created with its run.

    ``definition`` is the window's report: its first year, its size and the
    sides and options every year shares. ``population`` is the first
    year's, on the current catalog version; each year calculates on the
    same catalog version, region and dataset in its own year. ``inputs`` is
    what ``reports.inputs`` stores (the SPM selection, answer 4). ``years``
    runs from the first year, one at a time.
    """

    definition: EconomyReportDefinition
    report_id: UUID
    window_run_id: UUID
    tax_benefit_model_id: UUID
    population: EconomyPopulation
    versions: RunVersions
    inputs: dict[str, Any]
    years: tuple[KeyedWindowYear, ...]

    def __post_init__(self) -> None:
        """Refuse years that aren't the definition's, 1 to 75 from its first."""
        check_window_years(self.population.year, [year.year for year in self.years])
        if (self.definition.year, self.definition.budget_window_size) != (
            self.population.year,
            len(self.years),
        ):
            msg = "a window's definition names its first year and its length"
            raise ValueError(msg)

    def population_in(self, year: int) -> EconomyPopulation:
        """Return what the window's simulations of ``year`` calculate on."""
        return replace(self.population, year=year)


@dataclass(frozen=True)
class FoundWindowYear:
    """A year of the window run a request ended on: its report run and sides.

    Each side's ``run_inserted`` says whether this request inserted the
    run; a joined window's years inserted none.
    """

    year: int
    report_run_id: UUID
    baseline: FoundSide
    reform: FoundSide


@dataclass(frozen=True)
class FoundBudgetWindow:
    """The window report and window run a request ended on, and its years."""

    report_id: UUID
    report_inserted: bool
    window_run: FoundWindowRun
    years: tuple[FoundWindowYear, ...]


def find_or_create_budget_window(
    connection: Connection, request: KeyedBudgetWindow
) -> FoundBudgetWindow:
    """Find the window the request names, and its window run, or create them.

    Raise EconomyRecordReferenceError when the database refuses a row an
    insert names, and SharedRowNotSettledError when a run keeps failing
    between a lookup's insert and its read; either ends the caller's
    transaction, or its savepoint.
    """
    with references_checked():
        simulation_ids = _find_or_insert_simulations(connection, request)
        first_year = request.years[0].year
        report = find_or_insert_report(
            connection,
            economy_report_row(
                request.definition,
                report_id=request.report_id,
                tax_benefit_model_id=request.tax_benefit_model_id,
                population=request.population,
                inputs=request.inputs,
                simulation_ids={
                    side: simulation_ids[first_year, side]
                    for side in ("baseline", "reform")
                },
            ),
        )
        versions = request.versions
        joined = find_joinable_window_run(
            connection,
            report_id=report.id,
            tax_benefit_model_version_id=request.population.tax_benefit_model_version_id,
            policyengine_version=versions.policyengine_version,
        )
        if joined is None:
            # The window run first: a request inserting one another request
            # has inserted waits for it, then joins it with its year runs.
            window_run = find_or_insert_window_run(
                connection,
                window_run_row(
                    run_id=request.window_run_id,
                    report_id=report.id,
                    population=request.population,
                    versions=versions,
                    recalculate=False,
                ),
            )
            if window_run.inserted:
                years = insert_window_years(
                    connection,
                    _years_to_insert(request, simulation_ids),
                    report_id=report.id,
                    window_run_id=request.window_run_id,
                    population=request.population,
                    versions=versions,
                    forced=False,
                )
                return FoundBudgetWindow(
                    report_id=report.id,
                    report_inserted=report.inserted,
                    window_run=window_run,
                    years=years,
                )
            joined = window_run
        return FoundBudgetWindow(
            report_id=report.id,
            report_inserted=report.inserted,
            window_run=joined,
            years=stored_window_years(connection, joined.id, simulation_ids),
        )


def window_run_row(
    *,
    run_id: UUID,
    report_id: UUID,
    population: EconomyPopulation,
    versions: RunVersions,
    recalculate: bool,
) -> dict[str, Any]:
    """Return a window's new run as inserted, pending, with its .py version.

    ``recalculate`` marks a forced re-run's, which nothing joins. It names
    no trigger: each user's report-run record holds it (answer 6).
    """
    return {
        "id": run_id,
        "report_id": report_id,
        "tax_benefit_model_version_id": population.tax_benefit_model_version_id,
        "policyengine_version": versions.policyengine_version,
        "recalculate": recalculate,
        "status": ReportRunStatus.PENDING,
    }


def window_year_run_row(  # noqa: PLR0913  # the year's run, then what it calculates on
    *,
    run_id: UUID,
    report_id: UUID,
    window_run_id: UUID,
    year: int,
    side_runs: dict[Side, UUID],
    versions: RunVersions,
    forced: bool,
) -> dict[str, Any]:
    """Return a window year's report run as inserted, pending with no call."""
    return {
        "id": run_id,
        "report_id": report_id,
        "baseline_simulation_run_id": side_runs["baseline"],
        "reform_simulation_run_id": side_runs["reform"],
        "budget_window_run_id": window_run_id,
        "year": year,
        "country_package_version": versions.country_package_version,
        "policyengine_version": versions.policyengine_version,
        "status": ReportRunStatus.PENDING,
        "modal_call_id": None,
        "forced": forced,
    }


def _find_or_insert_simulations(
    connection: Connection, request: KeyedBudgetWindow
) -> dict[YearSide, UUID]:
    """Return each year's sides' simulation IDs, found by key or inserted.

    In key order across every year, so two transactions never wait for each
    other in opposite orders; two sides with one key name one simulation.
    """
    proposed: dict[YearSide, ProposedSide] = {}
    by_key: dict[str, list[YearSide]] = {}
    for window_year in request.years:
        for side, side_ids in _sides(window_year, reform_first=False):
            year_side: YearSide = (window_year.year, side)
            proposed[year_side] = side_ids
            key = request.definition.simulation(side, year=window_year.year)
            by_key.setdefault(key.definition_key, []).append(year_side)
    simulation_ids: dict[YearSide, UUID] = {}
    for key in sorted(by_key):
        year_sides = by_key[key]
        year, side = year_sides[0]
        found = find_or_insert_simulation(
            connection,
            economy_simulation_row(
                request.definition.simulation(side, year=year),
                proposed[year, side].simulation_id,
                request.population_in(year),
            ),
        )
        for year_side in year_sides:
            simulation_ids[year_side] = found.id
    return simulation_ids


def insert_window_years(  # noqa: PLR0913  # the years, then the window they belong to
    connection: Connection,
    years: Sequence[WindowYearToInsert],
    *,
    report_id: UUID,
    window_run_id: UUID,
    population: EconomyPopulation,
    versions: RunVersions,
    forced: bool,
) -> tuple[FoundWindowYear, ...]:
    """Name each year's side runs, then insert each year's report run, pending.

    Unforced, each distinct simulation's run on ``versions`` and its year's
    catalog rows (``population`` is the first year's) that hasn't failed
    and wasn't forced is found, or inserted with its side's proposed ID, in
    simulation ID order; one simulation on both sides of a year gets one
    run. Forced, each side gets a new run, marked forced like the year's
    report run. A keyed window's create and every window re-run share this.
    """
    if forced:
        for window_year in years:
            for side in _SIDE_NAMES:
                insert_simulation_run(
                    connection,
                    run_id=window_year.proposed_run_ids[side],
                    simulation_id=window_year.simulation_ids[side],
                    population=replace(population, year=window_year.year),
                    versions=versions,
                    forced=True,
                )
        found_years = tuple(
            _found_year(
                window_year,
                {
                    side: (window_year.proposed_run_ids[side], True)
                    for side in _SIDE_NAMES
                },
            )
            for window_year in years
        )
    else:
        rows: dict[UUID, dict[str, Any]] = {}
        for window_year in years:
            # The reform first, so one simulation on both sides keeps the
            # baseline's proposed ID.
            for side in reversed(_SIDE_NAMES):
                simulation_id = window_year.simulation_ids[side]
                rows[simulation_id] = simulation_run_row(
                    run_id=window_year.proposed_run_ids[side],
                    simulation_id=simulation_id,
                    population=replace(population, year=window_year.year),
                    versions=versions,
                )
        runs = find_or_insert_simulation_runs(connection, rows)
        found_years = tuple(
            _found_year(
                window_year,
                {
                    side: (
                        runs[window_year.simulation_ids[side]].id,
                        runs[window_year.simulation_ids[side]].inserted,
                    )
                    for side in _SIDE_NAMES
                },
            )
            for window_year in years
        )
    connection.execute(
        insert(report_runs),
        [
            window_year_run_row(
                run_id=year.report_run_id,
                report_id=report_id,
                window_run_id=window_run_id,
                year=year.year,
                side_runs={
                    "baseline": year.baseline.simulation_run_id,
                    "reform": year.reform.simulation_run_id,
                },
                versions=versions,
                forced=forced,
            )
            for year in found_years
        ],
    )
    return found_years


def _found_year(
    window_year: WindowYearToInsert, side_runs: dict[Side, tuple[UUID, bool]]
) -> FoundWindowYear:
    """Return a year as inserted, naming each side's run and who inserted it."""
    sides = {
        side: FoundSide(
            simulation_id=window_year.simulation_ids[side],
            simulation_run_id=side_runs[side][0],
            run_inserted=side_runs[side][1],
        )
        for side in _SIDE_NAMES
    }
    return FoundWindowYear(
        year=window_year.year,
        report_run_id=window_year.report_run_id,
        baseline=sides["baseline"],
        reform=sides["reform"],
    )


def _sides(
    window_year: KeyedWindowYear, *, reform_first: bool
) -> tuple[tuple[Side, ProposedSide], ...]:
    """Return the year's sides by name, the baseline's first unless asked."""
    sides: tuple[tuple[Side, ProposedSide], ...] = (
        ("baseline", window_year.baseline),
        ("reform", window_year.reform),
    )
    return sides[::-1] if reform_first else sides


def _years_to_insert(
    request: KeyedBudgetWindow, simulation_ids: dict[YearSide, UUID]
) -> tuple[WindowYearToInsert, ...]:
    """Return the request's years on the simulations their keys found."""
    return tuple(
        WindowYearToInsert(
            year=window_year.year,
            report_run_id=window_year.report_run_id,
            simulation_ids={
                side: simulation_ids[window_year.year, side] for side in _SIDE_NAMES
            },
            proposed_run_ids={
                side: side_ids.simulation_run_id
                for side, side_ids in _sides(window_year, reform_first=False)
            },
        )
        for window_year in request.years
    )


def stored_window_years(
    connection: Connection,
    window_run_id: UUID,
    simulation_ids: dict[YearSide, UUID],
) -> tuple[FoundWindowYear, ...]:
    """Return a window run's years, as its year runs name them, none inserted now.

    Raise ValueError when its years aren't the request's: a window run of
    one report always has them.
    """
    rows = connection.execute(
        select(
            report_runs.c.id,
            report_runs.c.year,
            report_runs.c.baseline_simulation_run_id,
            report_runs.c.reform_simulation_run_id,
        )
        .where(report_runs.c.budget_window_run_id == window_run_id)
        .order_by(report_runs.c.year)
    ).all()
    requested = sorted({year for year, _ in simulation_ids})
    if [row.year for row in rows] != requested:
        msg = f"window run {window_run_id} has other years than its report's"
        raise ValueError(msg)
    return tuple(
        FoundWindowYear(
            year=row.year,
            report_run_id=row.id,
            baseline=FoundSide(
                simulation_id=simulation_ids[row.year, "baseline"],
                simulation_run_id=row.baseline_simulation_run_id,
                run_inserted=False,
            ),
            reform=FoundSide(
                simulation_id=simulation_ids[row.year, "reform"],
                simulation_run_id=row.reform_simulation_run_id,
                run_inserted=False,
            ),
        )
        for row in rows
    )
