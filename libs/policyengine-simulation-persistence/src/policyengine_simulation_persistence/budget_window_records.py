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
from typing import TYPE_CHECKING, Any, Final

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
    ReportRunStatus,
    ReportRunTrigger,
    budget_window_runs,
    report_runs,
    reports,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Connection

# The most years one budget window compares, as the Public API's check on
# ``reports.budget_window_size`` allows.
MAX_BUDGET_WINDOW_YEARS: Final = 75
# The last year any run may calculate.
LAST_RUN_YEAR: Final = 2200


@dataclass(frozen=True)
class BudgetWindowYear:
    """One year of a window: its report run and its two sides' runs."""

    year: int
    report_run_id: UUID
    baseline: ReportSimulation
    reform: ReportSimulation

    @property
    def new_simulation_run_ids(self) -> tuple[UUID, ...]:
        """Return the IDs of the simulation runs created for the year."""
        return tuple(
            side.simulation_run_id
            for side in (self.baseline, self.reform)
            if not side.reused
        )


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
        first_year = self.population.year
        expected = tuple(range(first_year, first_year + len(self.years)))
        if (
            not self.years
            or len(self.years) > MAX_BUDGET_WINDOW_YEARS
            or tuple(year.year for year in self.years) != expected
            or expected[-1] > LAST_RUN_YEAR
        ):
            msg = (
                "a budget window's years must be 1 to "
                f"{MAX_BUDGET_WINDOW_YEARS} consecutive years from its first, "
                f"ending by {LAST_RUN_YEAR}"
            )
            raise ValueError(msg)

    @property
    def first_year(self) -> BudgetWindowYear:
        """The window's first year, whose simulations the report names."""
        return self.years[0]

    def population_in(self, year: int) -> EconomyPopulation:
        """Return what the window's simulations of ``year`` calculate on."""
        return replace(self.population, year=year)


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
            [_year_run_row(report, window_year) for window_year in report.years],
        )


def _year_run_row(
    report: NewBudgetWindowReport, window_year: BudgetWindowYear
) -> dict[str, Any]:
    """Return a year's report run, pending with no call, as inserted."""
    return {
        "id": window_year.report_run_id,
        "report_id": report.id,
        "baseline_simulation_run_id": window_year.baseline.simulation_run_id,
        "reform_simulation_run_id": window_year.reform.simulation_run_id,
        "budget_window_run_id": report.window_run_id,
        "year": window_year.year,
        "country_package_version": report.versions.country_package_version,
        "policyengine_version": report.versions.policyengine_version,
        "status": ReportRunStatus.PENDING,
        "trigger": ReportRunTrigger.INITIAL,
        "modal_call_id": None,
    }
