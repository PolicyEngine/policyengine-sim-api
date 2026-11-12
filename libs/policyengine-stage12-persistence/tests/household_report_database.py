"""Seed household report runs as the simulation service creates them, for tests.

A household report calculates one stored household under a baseline policy
and, optionally, a reform. The simulation service creates a household
simulation and a pending simulation run for each side, the report, and its
pending report run naming those runs, with no Modal call recorded on any of
them. The seeder inserts the same rows as the seed role, through
``ReportRunSeeder``'s reflected tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from report_run_database import VERSIONS
from sqlalchemy import delete, insert

if TYPE_CHECKING:
    from report_run_database import ReportRunSeeder
    from sqlalchemy import Connection

HOUSEHOLD_YEAR = 2026


@dataclass(frozen=True)
class SeededHouseholdReportRun:
    """A pending household report run and the simulation runs it names.

    ``reform_simulation_run_id`` is None for a report of the baseline alone.
    """

    report_run_id: UUID
    baseline_simulation_run_id: UUID
    reform_simulation_run_id: UUID | None
    report_id: UUID
    household_id: UUID
    model_id: UUID


def seed_household_report_run(
    seeder: ReportRunSeeder, *, with_reform: bool = True
) -> SeededHouseholdReportRun:
    """Insert a household report whose report run and simulation runs are pending."""
    model_id, version_id = uuid4(), uuid4()
    household_id, report_id, report_run_id = uuid4(), uuid4(), uuid4()
    sides = 2 if with_reform else 1
    simulation_ids = [uuid4() for _ in range(sides)]
    simulation_run_ids = [uuid4() for _ in range(sides)]
    with seeder.engine.begin() as connection:
        _insert_catalog_and_household(
            seeder, connection, model_id, version_id, household_id
        )
        for simulation_id, run_id in zip(
            simulation_ids, simulation_run_ids, strict=True
        ):
            connection.execute(
                insert(seeder.table("simulations")).values(
                    id=simulation_id,
                    simulation_type="household",
                    household_id=household_id,
                    tax_benefit_model_version_id=version_id,
                    year=HOUSEHOLD_YEAR,
                )
            )
            connection.execute(
                insert(seeder.table("simulation_runs")).values(
                    id=run_id,
                    simulation_id=simulation_id,
                    year=HOUSEHOLD_YEAR,
                    tax_benefit_model_version_id=version_id,
                    status="pending",
                    **VERSIONS,
                )
            )
        reform_simulation_id = simulation_ids[1] if with_reform else None
        reform_run_id = simulation_run_ids[1] if with_reform else None
        connection.execute(
            insert(seeder.table("reports")).values(
                id=report_id,
                label="Household report run test",
                country="us",
                type="household",
                tax_benefit_model_id=model_id,
                household_id=household_id,
                baseline_simulation_id=simulation_ids[0],
                reform_simulation_id=reform_simulation_id,
                year=HOUSEHOLD_YEAR,
                # A household report has no options.
                inputs={},
            )
        )
        connection.execute(
            insert(seeder.table("report_runs")).values(
                id=report_run_id,
                report_id=report_id,
                baseline_simulation_run_id=simulation_run_ids[0],
                reform_simulation_run_id=reform_run_id,
                status="pending",
                trigger="initial",
                **VERSIONS,
            )
        )
    return SeededHouseholdReportRun(
        report_run_id=report_run_id,
        baseline_simulation_run_id=simulation_run_ids[0],
        reform_simulation_run_id=reform_run_id,
        report_id=report_id,
        household_id=household_id,
        model_id=model_id,
    )


def remove_household_report_run(
    seeder: ReportRunSeeder, seeded: SeededHouseholdReportRun
) -> None:
    """Delete the seeded report, its runs, simulations, household and catalog rows."""
    report_runs = seeder.table("report_runs")
    simulation_runs = seeder.table("simulation_runs")
    simulations = seeder.table("simulations")
    reports = seeder.table("reports")
    households = seeder.table("households")
    models = seeder.table("tax_benefit_models")
    simulation_run_ids = [
        run_id
        for run_id in (
            seeded.baseline_simulation_run_id,
            seeded.reform_simulation_run_id,
        )
        if run_id is not None
    ]
    with seeder.engine.begin() as connection:
        connection.execute(
            delete(report_runs).where(report_runs.c.id == seeded.report_run_id)
        )
        connection.execute(
            delete(simulation_runs).where(simulation_runs.c.id.in_(simulation_run_ids))
        )
        connection.execute(delete(reports).where(reports.c.id == seeded.report_id))
        connection.execute(
            delete(simulations).where(simulations.c.household_id == seeded.household_id)
        )
        connection.execute(
            delete(households).where(households.c.id == seeded.household_id)
        )
        # Deleting the model removes its versions.
        connection.execute(delete(models).where(models.c.id == seeded.model_id))


def _insert_catalog_and_household(
    seeder: ReportRunSeeder,
    connection: Connection,
    model_id: UUID,
    version_id: UUID,
    household_id: UUID,
) -> None:
    connection.execute(
        insert(seeder.table("tax_benefit_models")).values(
            id=model_id, name=f"test-{model_id.hex[:24]}"
        )
    )
    connection.execute(
        insert(seeder.table("tax_benefit_model_versions")).values(
            id=version_id,
            model_id=model_id,
            version="1.0.0",
            current_law_id=1,
            metadata_time_periods={},
        )
    )
    connection.execute(
        insert(seeder.table("households")).values(
            id=household_id,
            household_data={"people": {"you": {"age": {"2026": 40}}}},
            country_id="us",
            default_year=HOUSEHOLD_YEAR,
            canonicalization_version=1,
            # Unique per household: households are deduplicated by it.
            content_hash=household_id.hex * 2,
        )
    )
