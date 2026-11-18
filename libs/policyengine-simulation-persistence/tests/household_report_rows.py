"""Household reports stored as the simulation service created them before keys.

The service now finds or creates a household report by definition key
(``keyed_household_records``). A report it created before then has no key:
each side is a household simulation of its own with one run, the report
names the household, both simulations and the reform's policy, and its
first run has trigger ``initial``. Every run is ``pending`` with no Modal
call ID. ``insert_household_report_rows`` inserts such a report directly,
as whatever role the connection acts as.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from policyengine_simulation_persistence import (
    HOUSEHOLD_REPORT_TYPE,
    Report,
    ReportRun,
    ReportRunStatus,
    ReportRunTrigger,
    Simulation,
    SimulationRun,
    SimulationStatus,
    SimulationType,
)
from sqlalchemy import insert

if TYPE_CHECKING:
    from uuid import UUID

    from policyengine_simulation_persistence import RunVersions
    from public_api_rows import CatalogVersion
    from sqlalchemy import Connection


@dataclass(frozen=True)
class StoredHouseholdSide:
    """One side of a stored household report: its simulation, run and policy.

    ``policy_id`` is the side's stored policy, or None for current law.
    """

    simulation_id: UUID
    simulation_run_id: UUID
    policy_id: UUID | None


@dataclass(frozen=True)
class StoredHouseholdReport:
    """A stored household report and its first run.

    ``reform`` is None for a report of the baseline alone.
    """

    id: UUID
    run_id: UUID
    household_id: UUID
    year: int
    baseline: StoredHouseholdSide
    reform: StoredHouseholdSide | None

    @property
    def sides(self) -> tuple[StoredHouseholdSide, ...]:
        """Return each side the report calculates, the baseline's first."""
        return (self.baseline,) if self.reform is None else (self.baseline, self.reform)

    @property
    def simulation_run_ids(self) -> tuple[UUID, ...]:
        """Return the IDs of the report's first simulation runs."""
        return tuple(side.simulation_run_id for side in self.sides)


def insert_household_report_rows(  # noqa: PLR0913  # the report's catalog, household and sides
    connection: Connection,
    catalog: CatalogVersion,
    *,
    household_id: UUID,
    year: int,
    versions: RunVersions,
    reform_policy_id: UUID | None = None,
    with_reform: bool = True,
) -> StoredHouseholdReport:
    """Insert a household report with no key, its simulations and its runs.

    The baseline is current law; with ``with_reform``, the reform has
    ``reform_policy_id``, current law when it is None.
    """
    report = StoredHouseholdReport(
        id=uuid4(),
        run_id=uuid4(),
        household_id=household_id,
        year=year,
        baseline=StoredHouseholdSide(
            simulation_id=uuid4(), simulation_run_id=uuid4(), policy_id=None
        ),
        reform=(
            StoredHouseholdSide(
                simulation_id=uuid4(),
                simulation_run_id=uuid4(),
                policy_id=reform_policy_id,
            )
            if with_reform
            else None
        ),
    )
    for side in report.sides:
        connection.execute(
            insert(Simulation).values(
                id=side.simulation_id,
                simulation_type=SimulationType.HOUSEHOLD,
                dataset_id=None,
                household_id=household_id,
                policy_id=side.policy_id,
                dynamic_id=None,
                tax_benefit_model_version_id=catalog.version_id,
                region_id=None,
                year=year,
            )
        )
        connection.execute(
            insert(SimulationRun).values(
                _pending_run(side, catalog=catalog, year=year, versions=versions)
            )
        )
    reform = report.reform
    connection.execute(
        insert(Report).values(
            id=report.id,
            label="A larger child tax credit",
            description="For one family",
            country=catalog.country,
            type=HOUSEHOLD_REPORT_TYPE,
            user_id=None,
            tax_benefit_model_id=catalog.model_id,
            policy_id=None if reform is None else reform.policy_id,
            baseline_simulation_id=report.baseline.simulation_id,
            reform_simulation_id=None if reform is None else reform.simulation_id,
            household_id=household_id,
            dataset_id=None,
            region_id=None,
            year=year,
            budget_window_size=None,
            inputs={},
        )
    )
    connection.execute(
        insert(ReportRun).values(
            id=report.run_id,
            report_id=report.id,
            baseline_simulation_run_id=report.baseline.simulation_run_id,
            reform_simulation_run_id=(
                None if reform is None else reform.simulation_run_id
            ),
            budget_window_run_id=None,
            year=None,
            country_package_version=versions.country_package_version,
            policyengine_version=versions.policyengine_version,
            status=ReportRunStatus.PENDING,
            trigger=ReportRunTrigger.INITIAL,
            modal_call_id=None,
        )
    )
    return report


def _pending_run(
    side: StoredHouseholdSide,
    *,
    catalog: CatalogVersion,
    year: int,
    versions: RunVersions,
) -> dict[str, Any]:
    """Return a side's household simulation run, pending with no Modal call ID."""
    return {
        "id": side.simulation_run_id,
        "simulation_id": side.simulation_id,
        "year": year,
        "tax_benefit_model_version_id": catalog.version_id,
        "country_package_version": versions.country_package_version,
        "policyengine_version": versions.policyengine_version,
        "dataset_id": None,
        "region_id": None,
        "status": SimulationStatus.PENDING,
        "modal_call_id": None,
        "forced": False,
    }
