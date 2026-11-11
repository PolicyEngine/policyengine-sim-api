"""A disposable Postgres holding the Public API's v2 tables, for report run tests.

The tests run only when both URLs name a disposable database that the Public
API's v2 migrations created and whose declared privileges were applied:

- ``REPORT_RUN_TEST_DATABASE_URL`` logs in as the Modal worker role, which the
  store under test uses;
- ``REPORT_RUN_TEST_SEED_DATABASE_URL`` logs in as a role that may insert the
  rows the simulation service would create, so each test can set them up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from sqlalchemy import Connection, Engine, MetaData, Table, delete, insert, select

WORKER_URL_ENV = "REPORT_RUN_TEST_DATABASE_URL"
SEED_URL_ENV = "REPORT_RUN_TEST_SEED_DATABASE_URL"
# The foreign keys between the Public API's tables and the simulation API's
# stay, so every catalog row a seeded run names exists.
SEEDED_TABLES = (
    "tax_benefit_models",
    "tax_benefit_model_versions",
    "datasets",
    "simulations",
    "reports",
    "report_runs",
    "simulation_runs",
    "labor_supply_responses",
    "labor_supply_decile_responses",
    "budget_summary",
    "decile_impacts",
    "intra_decile_impacts",
    "poverty",
    "inequality",
    "program_statistics",
    "congressional_district_impacts",
    "constituency_impacts",
    "local_authority_impacts",
    "households",
    "household_jobs",
)
NOW = datetime(2026, 11, 4, tzinfo=UTC)
VERSIONS = {"country_package_version": "1.0.0", "policyengine_version": "6.12.0"}


def worker_database_url() -> str:
    """Return the Modal worker role's test URL, or skip when none is set."""
    url = os.environ.get(WORKER_URL_ENV)
    if not url or not os.environ.get(SEED_URL_ENV):
        pytest.skip(f"{WORKER_URL_ENV} and {SEED_URL_ENV} name no test database")
    return url


@dataclass(frozen=True)
class SeededReportRun:
    """A report run and its two simulation runs, as the simulation service makes."""

    run_ids: ReportRunIds
    simulation_ids: tuple[UUID, UUID]
    model_id: UUID


@dataclass(frozen=True)
class SeededHouseholdJob:
    """A pending household job and the household it calculates."""

    job_id: UUID
    household_id: UUID
    model_id: UUID


class ReportRunSeeder:
    """Insert and read back report runs as the seed role."""

    def __init__(self, engine: Engine) -> None:
        """Reflect the seeded tables through ``engine``; this reads, never alters."""
        self.engine = engine
        self.metadata = MetaData(schema="public")
        self.metadata.reflect(engine, only=list(SEEDED_TABLES))

    def table(self, name: str) -> Table:
        """Return the reflected table ``name``."""
        return self.metadata.tables[f"public.{name}"]

    def seed_report_run(self) -> SeededReportRun:
        """Insert a pending report run whose baseline and reform runs are pending."""
        model_id, version_id, dataset_id = uuid4(), uuid4(), uuid4()
        simulation_ids = (uuid4(), uuid4())
        report_id = uuid4()
        run_ids = ReportRunIds(
            report_run_id=uuid4(),
            baseline_simulation_run_id=uuid4(),
            reform_simulation_run_id=uuid4(),
        )
        with self.engine.begin() as connection:
            self._insert_catalog(connection, model_id, version_id, dataset_id)
            for simulation_id in simulation_ids:
                connection.execute(
                    insert(self.table("simulations")).values(
                        id=simulation_id,
                        simulation_type="economy",
                        dataset_id=dataset_id,
                        tax_benefit_model_version_id=version_id,
                        year=2026,
                    )
                )
            connection.execute(
                insert(self.table("reports")).values(
                    id=report_id,
                    label="Report run test",
                    country="us",
                    tax_benefit_model_id=model_id,
                    baseline_simulation_id=simulation_ids[0],
                    reform_simulation_id=simulation_ids[1],
                    year=2026,
                    inputs={},
                )
            )
            for run_id, simulation_id in (
                (run_ids.baseline_simulation_run_id, simulation_ids[0]),
                (run_ids.reform_simulation_run_id, simulation_ids[1]),
            ):
                connection.execute(
                    insert(self.table("simulation_runs")).values(
                        id=run_id,
                        simulation_id=simulation_id,
                        year=2026,
                        tax_benefit_model_version_id=version_id,
                        dataset_id=dataset_id,
                        status="pending",
                        **VERSIONS,
                    )
                )
            connection.execute(
                insert(self.table("report_runs")).values(
                    id=run_ids.report_run_id,
                    report_id=report_id,
                    baseline_simulation_run_id=run_ids.baseline_simulation_run_id,
                    reform_simulation_run_id=run_ids.reform_simulation_run_id,
                    status="pending",
                    trigger="initial",
                    **VERSIONS,
                )
            )
        return SeededReportRun(
            run_ids=run_ids, simulation_ids=simulation_ids, model_id=model_id
        )

    def seed_household_job(self) -> SeededHouseholdJob:
        """Insert a pending household job with no Modal call, as the service does."""
        model_id, version_id, dataset_id = uuid4(), uuid4(), uuid4()
        household_id, job_id = uuid4(), uuid4()
        with self.engine.begin() as connection:
            self._insert_catalog(connection, model_id, version_id, dataset_id)
            connection.execute(
                insert(self.table("households")).values(
                    id=household_id,
                    household_data={"people": {"you": {"age": {"2026": 40}}}},
                    country_id="us",
                    default_year=2026,
                    canonicalization_version=1,
                    # Unique per household: households are deduplicated by it.
                    content_hash=household_id.hex * 2,
                )
            )
            connection.execute(
                insert(self.table("household_jobs")).values(
                    id=job_id,
                    country="us",
                    household_id=household_id,
                    year=2026,
                    request_data={"household_id": str(household_id)},
                    tax_benefit_model_version_id=version_id,
                    status="pending",
                    **VERSIONS,
                )
            )
        return SeededHouseholdJob(
            job_id=job_id, household_id=household_id, model_id=model_id
        )

    def remove_household_job(self, seeded: SeededHouseholdJob) -> None:
        """Delete the seeded job, its household and its catalog rows."""
        jobs = self.table("household_jobs")
        households = self.table("households")
        datasets = self.table("datasets")
        versions = self.table("tax_benefit_model_versions")
        models = self.table("tax_benefit_models")
        version_ids = select(versions.c.id).where(
            versions.c.model_id == seeded.model_id
        )
        with self.engine.begin() as connection:
            connection.execute(delete(jobs).where(jobs.c.id == seeded.job_id))
            connection.execute(
                delete(households).where(households.c.id == seeded.household_id)
            )
            connection.execute(
                delete(datasets).where(
                    datasets.c.tax_benefit_model_version_id.in_(version_ids)
                )
            )
            # Deleting the model removes its versions.
            connection.execute(delete(models).where(models.c.id == seeded.model_id))

    def _insert_catalog(
        self, connection: Connection, model_id: UUID, version_id: UUID, dataset_id: UUID
    ) -> None:
        connection.execute(
            insert(self.table("tax_benefit_models")).values(
                id=model_id, name=f"test-{model_id.hex[:24]}"
            )
        )
        connection.execute(
            insert(self.table("tax_benefit_model_versions")).values(
                id=version_id,
                model_id=model_id,
                version="1.0.0",
                current_law_id=1,
                metadata_time_periods={},
            )
        )
        connection.execute(
            insert(self.table("datasets")).values(
                id=dataset_id,
                name="report-run-test",
                year=2026,
                is_output_dataset=False,
                tax_benefit_model_version_id=version_id,
            )
        )

    def row(self, table_name: str, row_id: UUID) -> dict[str, Any]:
        """Return the row ``row_id`` of ``table_name`` as a dictionary."""
        table = self.table(table_name)
        with self.engine.connect() as connection:
            row = connection.execute(select(table).where(table.c.id == row_id)).one()
        return dict(row._mapping)

    def rows(self, table_name: str, **filters: object) -> list[dict[str, Any]]:
        """Return the rows of ``table_name`` whose columns equal ``filters``."""
        table = self.table(table_name)
        statement = select(table).where(
            *(table.c[name] == value for name, value in filters.items())
        )
        with self.engine.connect() as connection:
            return [dict(row._mapping) for row in connection.execute(statement)]

    def remove(self, seeded: SeededReportRun) -> None:
        """Delete the seeded rows; the runs and their results cascade."""
        reports = self.table("reports")
        simulations = self.table("simulations")
        datasets = self.table("datasets")
        versions = self.table("tax_benefit_model_versions")
        models = self.table("tax_benefit_models")
        version_ids = select(versions.c.id).where(
            versions.c.model_id == seeded.model_id
        )
        with self.engine.begin() as connection:
            connection.execute(
                delete(reports).where(reports.c.tax_benefit_model_id == seeded.model_id)
            )
            connection.execute(
                delete(simulations).where(simulations.c.id.in_(seeded.simulation_ids))
            )
            connection.execute(
                delete(datasets).where(
                    datasets.c.tax_benefit_model_version_id.in_(version_ids)
                )
            )
            # Deleting the model removes its versions.
            connection.execute(delete(models).where(models.c.id == seeded.model_id))
