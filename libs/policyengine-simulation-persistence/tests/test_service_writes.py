"""The simulation service's role writes its tables in the Public API's database.

These tests run against a database the Public API's own v2 migration chain
built, with the privileges its manifest declares granted
(``PUBLIC_API_SCHEMA_DATABASE_URL``; see ``conftest``). Each test works in
one transaction as the service's role and rolls it back, so the database
stays as the chain built it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import connection_as_the_service
from policyengine_simulation_persistence import (
    HouseholdJobStatus,
    ReportRunStatus,
    ReportRunTrigger,
    SimulationRole,
    SimulationStatus,
    SimulationType,
    household_jobs,
    missing_table_privileges,
    report_run_simulation_runs,
    report_runs,
    reports,
    simulation_runs,
    simulations,
)
from sqlalchemy import func, insert, select

pytestmark = pytest.mark.public_api_schema

COUNTRY_PACKAGE_VERSION = "1.402.0"
POLICYENGINE_VERSION = "3.4.0"


def test_the_service_role_holds_every_privilege_the_service_needs(
    public_api_database_url,
) -> None:
    with connection_as_the_service(public_api_database_url) as connection:
        assert missing_table_privileges(connection) == []


def test_the_service_role_creates_and_reads_a_row_in_every_table_it_writes(
    public_api_database_url,
) -> None:
    model_version_id = uuid4()
    with connection_as_the_service(public_api_database_url) as connection:
        simulation_id = connection.execute(
            insert(simulations)
            .values(
                simulation_type=SimulationType.ECONOMY,
                dataset_id=uuid4(),
                tax_benefit_model_version_id=model_version_id,
                year=2026,
            )
            .returning(simulations.c.id)
        ).scalar_one()
        report_id = connection.execute(
            insert(reports)
            .values(
                label="Test report",
                country="us",
                tax_benefit_model_id=uuid4(),
                baseline_simulation_id=simulation_id,
                year=2026,
                inputs={},
            )
            .returning(reports.c.id)
        ).scalar_one()
        simulation_run_id = connection.execute(
            insert(simulation_runs)
            .values(
                simulation_id=simulation_id,
                tax_benefit_model_version_id=model_version_id,
                country_package_version=COUNTRY_PACKAGE_VERSION,
                policyengine_version=POLICYENGINE_VERSION,
                status=SimulationStatus.PENDING,
            )
            .returning(simulation_runs.c.id)
        ).scalar_one()
        report_run_id = connection.execute(
            insert(report_runs)
            .values(
                report_id=report_id,
                country_package_version=COUNTRY_PACKAGE_VERSION,
                policyengine_version=POLICYENGINE_VERSION,
                status=ReportRunStatus.PENDING,
                trigger=ReportRunTrigger.INITIAL,
            )
            .returning(report_runs.c.id)
        ).scalar_one()
        connection.execute(
            insert(report_run_simulation_runs).values(
                report_run_id=report_run_id,
                simulation_run_id=simulation_run_id,
                role=SimulationRole.BASELINE,
            )
        )
        connection.execute(
            insert(household_jobs).values(
                country="us",
                request_data={},
                status=HouseholdJobStatus.PENDING,
                household_id=uuid4(),
                year=2026,
                tax_benefit_model_version_id=model_version_id,
                country_package_version=COUNTRY_PACKAGE_VERSION,
                policyengine_version=POLICYENGINE_VERSION,
            )
        )

        assert (
            connection.execute(
                select(simulation_runs.c.status).where(
                    simulation_runs.c.id == simulation_run_id
                )
            ).scalar_one()
            == SimulationStatus.PENDING
        )
        assert (
            connection.execute(
                select(report_run_simulation_runs.c.role).where(
                    report_run_simulation_runs.c.report_run_id == report_run_id
                )
            ).scalar_one()
            == SimulationRole.BASELINE
        )
        for table in (simulations, reports, report_runs, household_jobs):
            assert (
                connection.execute(select(func.count()).select_from(table)).scalar_one()
                >= 1
            ), table.name
