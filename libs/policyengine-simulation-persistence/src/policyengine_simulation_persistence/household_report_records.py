"""The simulation service's writes for household reports.

A household report calculates one stored household, for one year, under a
baseline policy and, optionally, a reform. The service creates in one
transaction:

- for each side, a new household ``simulations`` row naming the household
  and the side's policy, and its first ``simulation_runs`` row, ``pending``
  with no Modal call ID. Household runs are never reused: each report gets
  its own;
- the ``reports`` row, of type ``household``, naming the household, both
  simulations and the reform's policy, with the report's options in
  ``inputs``;
- its first ``report_runs`` row, ``pending`` with no Modal call ID and
  trigger ``initial``, naming both simulation runs, or the baseline's
  alone.

The service then starts one household function call for the report run and
records the call's ID on it, or fails it and its simulation runs at once,
with ``economy_run_records``' report run writes. The household function
writes the rest: each run's ``household_result``, and every status after
``pending``. A re-run (``economy_rerun_records``) adds a run to each of a
stored report's household simulations with ``insert_household_simulation_run``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import insert

from policyengine_simulation_persistence.economy_run_records import (
    references_checked,
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

    from sqlalchemy import Connection

    from policyengine_simulation_persistence.economy_run_records import RunVersions

# A report's type, as the Public API reads it.
HOUSEHOLD_REPORT_TYPE: Final = "household"


@dataclass(frozen=True)
class HouseholdReportSimulation:
    """The new household simulation and run one side of a report creates.

    ``policy_id`` is the side's stored policy, or None for current law.
    """

    simulation_id: UUID
    simulation_run_id: UUID
    policy_id: UUID | None


@dataclass(frozen=True)
class NewHouseholdReport:
    """A household report to create, with its first run.

    ``reform`` is None for a report of the baseline alone. ``inputs`` holds
    the report's options as the Public API reads them.
    """

    id: UUID
    run_id: UUID
    country: str
    label: str
    description: str | None
    tax_benefit_model_id: UUID
    tax_benefit_model_version_id: UUID
    household_id: UUID
    year: int
    versions: RunVersions
    inputs: dict[str, Any]
    baseline: HouseholdReportSimulation
    reform: HouseholdReportSimulation | None

    @property
    def sides(self) -> tuple[HouseholdReportSimulation, ...]:
        """Return each side the report calculates, the baseline's first."""
        return (self.baseline,) if self.reform is None else (self.baseline, self.reform)

    @property
    def simulation_run_ids(self) -> tuple[UUID, ...]:
        """Return the IDs of the simulation runs created for the report."""
        return tuple(side.simulation_run_id for side in self.sides)


def insert_household_report(connection: Connection, report: NewHouseholdReport) -> None:
    """Create the report, its first run and each side's simulation and run.

    Raise EconomyRecordReferenceError when the database refuses a row it
    names. The refusal ends the caller's transaction, or its savepoint.
    """
    with references_checked():
        for side in report.sides:
            _insert_household_simulation(connection, report, side)
        reform = report.reform
        connection.execute(
            insert(reports).values(
                id=report.id,
                label=report.label,
                description=report.description,
                country=report.country,
                type=HOUSEHOLD_REPORT_TYPE,
                user_id=None,
                tax_benefit_model_id=report.tax_benefit_model_id,
                policy_id=None if reform is None else reform.policy_id,
                baseline_simulation_id=report.baseline.simulation_id,
                reform_simulation_id=None if reform is None else reform.simulation_id,
                household_id=report.household_id,
                dataset_id=None,
                region_id=None,
                year=report.year,
                budget_window_size=None,
                inputs=report.inputs,
            )
        )
        connection.execute(
            insert(report_runs).values(
                id=report.run_id,
                report_id=report.id,
                baseline_simulation_run_id=report.baseline.simulation_run_id,
                reform_simulation_run_id=(
                    None if reform is None else reform.simulation_run_id
                ),
                budget_window_run_id=None,
                year=None,
                country_package_version=report.versions.country_package_version,
                policyengine_version=report.versions.policyengine_version,
                status=ReportRunStatus.PENDING,
                trigger=ReportRunTrigger.INITIAL,
                modal_call_id=None,
            )
        )


def _insert_household_simulation(
    connection: Connection, report: NewHouseholdReport, side: HouseholdReportSimulation
) -> None:
    """Create one side's household simulation and its first run, pending."""
    connection.execute(
        insert(simulations).values(
            id=side.simulation_id,
            simulation_type=SimulationType.HOUSEHOLD,
            dataset_id=None,
            household_id=report.household_id,
            policy_id=side.policy_id,
            dynamic_id=None,
            tax_benefit_model_version_id=report.tax_benefit_model_version_id,
            region_id=None,
            year=report.year,
        )
    )
    insert_household_simulation_run(
        connection,
        run_id=side.simulation_run_id,
        simulation_id=side.simulation_id,
        year=report.year,
        tax_benefit_model_version_id=report.tax_benefit_model_version_id,
        versions=report.versions,
    )


def insert_household_simulation_run(  # noqa: PLR0913  # the run and what it calculates on
    connection: Connection,
    *,
    run_id: UUID,
    simulation_id: UUID,
    year: int,
    tax_benefit_model_version_id: UUID,
    versions: RunVersions,
    forced: bool = False,
) -> None:
    """Create a run of a household simulation, pending with no Modal call ID.

    It records the year, catalog version and versions it calculates on; a
    household run names no dataset or region. A ``forced`` run is an extra
    run that nothing joins.
    """
    connection.execute(
        insert(simulation_runs).values(
            {
                **household_simulation_run_row(
                    run_id=run_id,
                    simulation_id=simulation_id,
                    year=year,
                    tax_benefit_model_version_id=tax_benefit_model_version_id,
                    versions=versions,
                ),
                "forced": forced,
            }
        )
    )


def household_simulation_run_row(
    *,
    run_id: UUID,
    simulation_id: UUID,
    year: int,
    tax_benefit_model_version_id: UUID,
    versions: RunVersions,
) -> dict[str, Any]:
    """Return a household simulation's new run, pending with no Modal call ID.

    Unforced: ``insert_household_simulation_run`` adds ``forced`` to it.
    """
    return {
        "id": run_id,
        "simulation_id": simulation_id,
        "year": year,
        "tax_benefit_model_version_id": tax_benefit_model_version_id,
        "country_package_version": versions.country_package_version,
        "policyengine_version": versions.policyengine_version,
        "dataset_id": None,
        "region_id": None,
        "status": SimulationStatus.PENDING,
        "modal_call_id": None,
    }
