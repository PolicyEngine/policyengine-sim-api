"""Rows the simulation service creates, for tests of the roles' writes.

The tests run against a database the Public API's own v2 migration chain
built (``PUBLIC_API_SCHEMA_DATABASE_URL``; see ``conftest``), inside a
transaction they roll back.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from policyengine_simulation_persistence import (
    HouseholdJobStatus,
    ReportRunStatus,
    ReportRunTrigger,
    SimulationStatus,
    SimulationType,
    budget_window_runs,
    household_jobs,
    report_runs,
    reports,
    simulation_runs,
    simulations,
)
from sqlalchemy import insert, text

if TYPE_CHECKING:
    from sqlalchemy import Connection

COUNTRY_PACKAGE_VERSION = "1.402.0"
POLICYENGINE_VERSION = "3.4.0"
YEAR = 2026


@dataclass(frozen=True)
class PublicApiRows:
    """Rows of the Public API's own tables that the service's rows name."""

    tax_benefit_model_id: UUID
    model_version_id: UUID
    dataset_id: UUID
    household_id: UUID


@dataclass(frozen=True)
class ServiceRows:
    """A budget-window report's rows, one year's report run and a household job."""

    baseline_simulation_id: UUID
    reform_simulation_id: UUID
    report_id: UUID
    baseline_run_id: UUID
    reform_run_id: UUID
    budget_window_run_id: UUID
    report_run_id: UUID
    household_job_id: UUID


def insert_public_api_rows(connection: Connection) -> PublicApiRows:
    """Insert, as the superuser, the catalog, dataset and household rows a run names.

    The Public API writes these rows, and the database checks on insert that
    each row the service writes names ones that exist.
    """
    rows = PublicApiRows(
        tax_benefit_model_id=uuid4(),
        model_version_id=uuid4(),
        dataset_id=uuid4(),
        household_id=uuid4(),
    )
    connection.execute(
        text("INSERT INTO public.tax_benefit_models (id, name) VALUES (:id, :name)"),
        {
            "id": rows.tax_benefit_model_id,
            "name": f"test-{rows.tax_benefit_model_id.hex[:12]}",
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.tax_benefit_model_versions "
            "(id, model_id, version, current_law_id, metadata_time_periods) "
            "VALUES (:id, :model_id, :version, 1, '{}')"
        ),
        {
            "id": rows.model_version_id,
            "model_id": rows.tax_benefit_model_id,
            "version": COUNTRY_PACKAGE_VERSION,
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.datasets "
            "(id, name, year, is_output_dataset, tax_benefit_model_version_id) "
            "VALUES (:id, 'test dataset', :year, false, :model_version_id)"
        ),
        {
            "id": rows.dataset_id,
            "year": YEAR,
            "model_version_id": rows.model_version_id,
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.households "
            "(id, household_data, country_id, canonicalization_version, content_hash) "
            "VALUES (:id, '{}', 'us', 1, :content_hash)"
        ),
        {
            "id": rows.household_id,
            "content_hash": sha256(rows.household_id.bytes).hexdigest(),
        },
    )
    return rows


def insert_economy_simulation(
    connection: Connection, public_api_rows: PublicApiRows
) -> UUID:
    return connection.execute(
        insert(simulations)
        .values(
            simulation_type=SimulationType.ECONOMY,
            dataset_id=public_api_rows.dataset_id,
            tax_benefit_model_version_id=public_api_rows.model_version_id,
            year=YEAR,
        )
        .returning(simulations.c.id)
    ).scalar_one()


def insert_simulation_run(
    connection: Connection, simulation_id: UUID, public_api_rows: PublicApiRows
) -> UUID:
    return connection.execute(
        insert(simulation_runs)
        .values(
            simulation_id=simulation_id,
            year=YEAR,
            tax_benefit_model_version_id=public_api_rows.model_version_id,
            country_package_version=COUNTRY_PACKAGE_VERSION,
            policyengine_version=POLICYENGINE_VERSION,
            dataset_id=public_api_rows.dataset_id,
            status=SimulationStatus.PENDING,
        )
        .returning(simulation_runs.c.id)
    ).scalar_one()


def insert_service_rows(
    connection: Connection, public_api_rows: PublicApiRows
) -> ServiceRows:
    """Insert a row in every table the service writes, as a submission would.

    That is a two-year budget-window report's simulations, the report, the
    first year's simulation runs, the budget-window run and that year's report
    run, and a household job, every run pending.
    """
    baseline_simulation_id = insert_economy_simulation(connection, public_api_rows)
    reform_simulation_id = insert_economy_simulation(connection, public_api_rows)
    report_id = connection.execute(
        insert(reports)
        .values(
            label="Test report",
            country="us",
            tax_benefit_model_id=public_api_rows.tax_benefit_model_id,
            baseline_simulation_id=baseline_simulation_id,
            reform_simulation_id=reform_simulation_id,
            dataset_id=public_api_rows.dataset_id,
            year=YEAR,
            budget_window_size=2,
            inputs={},
        )
        .returning(reports.c.id)
    ).scalar_one()
    baseline_run_id = insert_simulation_run(
        connection, baseline_simulation_id, public_api_rows
    )
    reform_run_id = insert_simulation_run(
        connection, reform_simulation_id, public_api_rows
    )
    budget_window_run_id = connection.execute(
        insert(budget_window_runs)
        .values(
            report_id=report_id,
            tax_benefit_model_version_id=public_api_rows.model_version_id,
            trigger=ReportRunTrigger.INITIAL,
            recalculate=False,
            status=ReportRunStatus.PENDING,
        )
        .returning(budget_window_runs.c.id)
    ).scalar_one()
    report_run_id = connection.execute(
        insert(report_runs)
        .values(
            report_id=report_id,
            baseline_simulation_run_id=baseline_run_id,
            reform_simulation_run_id=reform_run_id,
            budget_window_run_id=budget_window_run_id,
            year=YEAR,
            country_package_version=COUNTRY_PACKAGE_VERSION,
            policyengine_version=POLICYENGINE_VERSION,
            status=ReportRunStatus.PENDING,
            trigger=ReportRunTrigger.INITIAL,
        )
        .returning(report_runs.c.id)
    ).scalar_one()
    household_job_id = connection.execute(
        insert(household_jobs)
        .values(
            country="us",
            request_data={},
            status=HouseholdJobStatus.PENDING,
            household_id=public_api_rows.household_id,
            year=YEAR,
            tax_benefit_model_version_id=public_api_rows.model_version_id,
            country_package_version=COUNTRY_PACKAGE_VERSION,
            policyengine_version=POLICYENGINE_VERSION,
        )
        .returning(household_jobs.c.id)
    ).scalar_one()
    return ServiceRows(
        baseline_simulation_id=baseline_simulation_id,
        reform_simulation_id=reform_simulation_id,
        report_id=report_id,
        baseline_run_id=baseline_run_id,
        reform_run_id=reform_run_id,
        budget_window_run_id=budget_window_run_id,
        report_run_id=report_run_id,
        household_job_id=household_job_id,
    )
