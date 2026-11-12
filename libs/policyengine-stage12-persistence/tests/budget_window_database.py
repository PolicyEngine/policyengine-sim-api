"""Budget-window runs in the disposable Postgres, as the simulation service makes them.

A budget-window report compares its baseline and reform in each year of its
window. The simulation service creates one ``budget_window_runs`` row per
calculation, and for each year two simulation runs and a report run naming
that window run and year, all pending with no Modal call. The seed role
inserts them; tests then set each year's status as a scenario needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from report_run_database import NOW, VERSIONS, ReportRunSeeder
from sqlalchemy import delete, insert, select

if TYPE_CHECKING:
    from collections.abc import Iterable

# Every seeded row was created and last changed at NOW, so a test's cutoffs
# don't depend on the clock.
SEEDED_AT = {"created_at": NOW, "updated_at": NOW}


@dataclass(frozen=True)
class SeededYear:
    """One year of a seeded window: its report run and its two simulation runs."""

    year: int
    report_run_id: UUID
    baseline_simulation_run_id: UUID
    reform_simulation_run_id: UUID


@dataclass(frozen=True)
class SeededWindow:
    """A seeded budget-window run, its report and its years, earliest first."""

    window_run_id: UUID
    report_id: UUID
    years: tuple[SeededYear, ...]
    simulation_ids: tuple[UUID, UUID]
    model_id: UUID

    def year(self, year: int) -> SeededYear:
        """Return the seeded year ``year``."""
        return next(seeded for seeded in self.years if seeded.year == year)


class BudgetWindowSeeder:
    """Insert, change and remove budget-window runs as the seed role."""

    def __init__(self, seeder: ReportRunSeeder) -> None:
        """Use ``seeder``'s reflected tables, which include ``budget_window_runs``."""
        self.seeder = seeder
        self.engine = seeder.engine

    def seed_window(self, first_year: int = 2026, size: int = 7) -> SeededWindow:
        """Insert a pending window run with ``size`` pending years from a year."""
        table = self.seeder.table
        model_id, version_id, dataset_id = uuid4(), uuid4(), uuid4()
        simulation_ids = (uuid4(), uuid4())
        report_id, window_run_id = uuid4(), uuid4()
        years = tuple(
            SeededYear(
                year=year,
                report_run_id=uuid4(),
                baseline_simulation_run_id=uuid4(),
                reform_simulation_run_id=uuid4(),
            )
            for year in range(first_year, first_year + size)
        )
        with self.engine.begin() as connection:
            self.seeder.insert_catalog(connection, model_id, version_id, dataset_id)
            for simulation_id in simulation_ids:
                connection.execute(
                    insert(table("simulations")).values(
                        id=simulation_id,
                        simulation_type="economy",
                        dataset_id=dataset_id,
                        tax_benefit_model_version_id=version_id,
                        year=first_year,
                    )
                )
            connection.execute(
                insert(table("reports")).values(
                    id=report_id,
                    label="Budget window test",
                    country="us",
                    tax_benefit_model_id=model_id,
                    baseline_simulation_id=simulation_ids[0],
                    reform_simulation_id=simulation_ids[1],
                    year=first_year,
                    budget_window_size=size,
                    inputs={},
                )
            )
            connection.execute(
                insert(table("budget_window_runs")).values(
                    id=window_run_id,
                    report_id=report_id,
                    tax_benefit_model_version_id=version_id,
                    trigger="initial",
                    recalculate=False,
                    status="pending",
                    **SEEDED_AT,
                )
            )
            for seeded in years:
                for run_id, simulation_id in (
                    (seeded.baseline_simulation_run_id, simulation_ids[0]),
                    (seeded.reform_simulation_run_id, simulation_ids[1]),
                ):
                    connection.execute(
                        insert(table("simulation_runs")).values(
                            id=run_id,
                            simulation_id=simulation_id,
                            year=seeded.year,
                            tax_benefit_model_version_id=version_id,
                            dataset_id=dataset_id,
                            status="pending",
                            **SEEDED_AT,
                            **VERSIONS,
                        )
                    )
                connection.execute(
                    insert(table("report_runs")).values(
                        id=seeded.report_run_id,
                        report_id=report_id,
                        baseline_simulation_run_id=seeded.baseline_simulation_run_id,
                        reform_simulation_run_id=seeded.reform_simulation_run_id,
                        budget_window_run_id=window_run_id,
                        year=seeded.year,
                        status="pending",
                        trigger="initial",
                        **SEEDED_AT,
                        **VERSIONS,
                    )
                )
        return SeededWindow(
            window_run_id=window_run_id,
            report_id=report_id,
            years=years,
            simulation_ids=simulation_ids,
            model_id=model_id,
        )

    def set_years(
        self, window: SeededWindow, years: Iterable[int], **values: Any
    ) -> None:  # column values of any type
        """Set ``values`` on the report runs of ``years`` in ``window``."""
        report_runs = self.seeder.table("report_runs")
        run_ids = [window.year(year).report_run_id for year in years]
        with self.engine.begin() as connection:
            connection.execute(
                report_runs.update()
                .where(report_runs.c.id.in_(run_ids))
                .values(**values)
            )

    def set_window(
        self, window: SeededWindow, **values: Any
    ) -> None:  # column values of any type
        """Set ``values`` on ``window``'s budget-window run."""
        windows = self.seeder.table("budget_window_runs")
        with self.engine.begin() as connection:
            connection.execute(
                windows.update()
                .where(windows.c.id == window.window_run_id)
                .values(**values)
            )

    def window_row(self, window: SeededWindow) -> dict[str, Any]:
        """Return ``window``'s budget-window run as a dictionary."""
        return self.seeder.row("budget_window_runs", window.window_run_id)

    def year_rows(self, window: SeededWindow) -> dict[int, dict[str, Any]]:
        """Return each year's report run by year."""
        return {
            row["year"]: row
            for row in self.seeder.rows(
                "report_runs", budget_window_run_id=window.window_run_id
            )
        }

    def simulation_run_statuses(self, seeded: SeededYear) -> tuple[str, str]:
        """Return the year's baseline and reform simulation runs' statuses."""
        simulation_runs = self.seeder.table("simulation_runs")
        statement = select(simulation_runs.c.id, simulation_runs.c.status).where(
            simulation_runs.c.id.in_(
                (seeded.baseline_simulation_run_id, seeded.reform_simulation_run_id)
            )
        )
        with self.engine.connect() as connection:
            statuses = {row.id: row.status for row in connection.execute(statement)}
        return (
            statuses[seeded.baseline_simulation_run_id],
            statuses[seeded.reform_simulation_run_id],
        )

    def remove(self, window: SeededWindow) -> None:
        """Delete the seeded rows; the window run, runs and results cascade."""
        table = self.seeder.table
        reports, simulations = table("reports"), table("simulations")
        datasets, models = table("datasets"), table("tax_benefit_models")
        versions = table("tax_benefit_model_versions")
        version_ids = select(versions.c.id).where(
            versions.c.model_id == window.model_id
        )
        with self.engine.begin() as connection:
            connection.execute(delete(reports).where(reports.c.id == window.report_id))
            connection.execute(
                delete(simulations).where(simulations.c.id.in_(window.simulation_ids))
            )
            connection.execute(
                delete(datasets).where(
                    datasets.c.tax_benefit_model_version_id.in_(version_ids)
                )
            )
            # Deleting the model removes its versions.
            connection.execute(delete(models).where(models.c.id == window.model_id))
