"""The rows of a budget-window report: one report run per year of its window.

A budget-window report compares the same baseline and reform in each year
of a window of 1 to 75 consecutive years. It is stored as an economy report
whose ``budget_window_size`` is the window's length, with one
``budget_window_runs`` row for its calculation and, for each year, a report
run naming that row and the year. Each year has its own baseline and reform
simulations, of that year, unless it reuses a run that already succeeded,
so a year's run can be reused by a single-year report, and the reverse. The
report names its first year's simulations.

The simulation service creates every row pending with no Modal call, then
starts the first years; each year's coordinator starts the next ones (see
``policyengine_stage12_persistence.BudgetWindowStore``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import insert

from policyengine_simulation_persistence.economy_run_records import (
    ECONOMY_REPORT_TYPE,
    EconomyPopulation,
    ReportSimulation,
    RunVersions,
    insert_simulation_with_run,
    references_checked,
)
from policyengine_simulation_persistence.tables import (
    LATEST_YEAR,
    MAX_BUDGET_WINDOW_YEARS,
    ReportRunStatus,
    ReportRunTrigger,
    budget_window_runs,
    report_runs,
    reports,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from sqlalchemy import Connection


class WindowSide(Protocol):
    """A window year's side: its simulation and the run the year names."""

    @property
    def simulation_id(self) -> UUID:
        """The side's simulation."""
        ...

    @property
    def simulation_run_id(self) -> UUID:
        """The run of it the year names."""
        ...

    @property
    def reused(self) -> bool:
        """Whether the run already succeeded, rather than being created."""
        ...


class WindowYearRuns(Protocol):
    """A year of a window run: its year, its report run and each side.

    A new window's ``BudgetWindowYear`` and a re-run's ``WindowRerunYear``
    are both one.
    """

    @property
    def year(self) -> int:
        """The year."""
        ...

    @property
    def report_run_id(self) -> UUID:
        """The year's report run."""
        ...

    @property
    def baseline(self) -> WindowSide:
        """The baseline side."""
        ...

    @property
    def reform(self) -> WindowSide:
        """The reform side."""
        ...


@dataclass(frozen=True)
class BudgetWindowYear:
    """One year of a window: its report run and its two sides' runs."""

    year: int
    report_run_id: UUID
    baseline: ReportSimulation
    reform: ReportSimulation


@dataclass(frozen=True)
class NewBudgetWindowReport:
    """A budget-window report to create, with its first calculation's runs.

    ``population`` is the first year's; each year's simulations use the same
    catalog version, region and dataset in their own year. ``inputs`` holds
    the report's options as the Public API reads them. ``years`` runs from
    the first year, one year at a time, earliest first.
    """

    id: UUID
    window_run_id: UUID
    country: str
    label: str
    description: str | None
    tax_benefit_model_id: UUID
    population: EconomyPopulation
    versions: RunVersions
    inputs: dict[str, Any]
    years: tuple[BudgetWindowYear, ...]

    def __post_init__(self) -> None:
        """Refuse years that aren't 1 to 75 consecutive ones from the first."""
        check_window_years(self.population.year, [year.year for year in self.years])

    @property
    def first_year(self) -> BudgetWindowYear:
        """The window's first year, whose simulations the report names."""
        return self.years[0]

    def population_in(self, year: int) -> EconomyPopulation:
        """Return what the window's simulations of ``year`` calculate on."""
        return replace(self.population, year=year)


def check_window_years(first_year: int, years: Sequence[int]) -> None:
    """Raise ValueError unless ``years`` are 1 to 75 in a row from ``first_year``.

    The last of them must also be no later than the tables' latest year.
    """
    expected = tuple(range(first_year, first_year + len(years)))
    if (
        not years
        or len(years) > MAX_BUDGET_WINDOW_YEARS
        or tuple(years) != expected
        or expected[-1] > LATEST_YEAR
    ):
        msg = (
            "a budget window's years must be 1 to "
            f"{MAX_BUDGET_WINDOW_YEARS} consecutive years from its first, "
            f"ending by {LATEST_YEAR}"
        )
        raise ValueError(msg)


def insert_budget_window_report(
    connection: Connection, report: NewBudgetWindowReport
) -> None:
    """Create the report, its budget-window run, and each year's runs, all pending.

    Each year's sides that reuse no run get a new simulation and run of that
    year. Raise EconomyRecordReferenceError when the database refuses a row
    it names. The refusal ends the caller's transaction, or its savepoint.
    """
    with references_checked():
        for window_year in report.years:
            population = report.population_in(window_year.year)
            for side in (window_year.baseline, window_year.reform):
                if not side.reused:
                    insert_simulation_with_run(
                        connection,
                        simulation_id=side.simulation_id,
                        run_id=side.simulation_run_id,
                        choice=side.choice,
                        population=population,
                        versions=report.versions,
                    )
        first_year = report.first_year
        population = report.population
        connection.execute(
            insert(reports).values(
                id=report.id,
                label=report.label,
                description=report.description,
                country=report.country,
                type=ECONOMY_REPORT_TYPE,
                user_id=None,
                tax_benefit_model_id=report.tax_benefit_model_id,
                policy_id=first_year.reform.choice.policy_id,
                baseline_simulation_id=first_year.baseline.simulation_id,
                reform_simulation_id=first_year.reform.simulation_id,
                household_id=None,
                dataset_id=population.dataset_id,
                region_id=population.region_id,
                year=population.year,
                budget_window_size=len(report.years),
                inputs=report.inputs,
            )
        )
        connection.execute(
            insert(budget_window_runs).values(
                id=report.window_run_id,
                report_id=report.id,
                tax_benefit_model_version_id=population.tax_benefit_model_version_id,
                trigger=ReportRunTrigger.INITIAL,
                recalculate=False,
                status=ReportRunStatus.PENDING,
            )
        )
        connection.execute(
            insert(report_runs),
            [
                year_run_row(
                    window_year,
                    report_id=report.id,
                    window_run_id=report.window_run_id,
                    versions=report.versions,
                    trigger=ReportRunTrigger.INITIAL,
                )
                for window_year in report.years
            ],
        )


def year_run_row(
    window_year: WindowYearRuns,
    *,
    report_id: UUID,
    window_run_id: UUID,
    versions: RunVersions,
    trigger: ReportRunTrigger,
) -> dict[str, Any]:
    """Return a year's report run in a window run, pending with no call, as inserted.

    ``window_year`` is a new window's year or a re-run's; ``trigger`` says
    which.
    """
    return {
        "id": window_year.report_run_id,
        "report_id": report_id,
        "baseline_simulation_run_id": window_year.baseline.simulation_run_id,
        "reform_simulation_run_id": window_year.reform.simulation_run_id,
        "budget_window_run_id": window_run_id,
        "year": window_year.year,
        "country_package_version": versions.country_package_version,
        "policyengine_version": versions.policyengine_version,
        "status": ReportRunStatus.PENDING,
        "trigger": trigger,
        "modal_call_id": None,
    }
