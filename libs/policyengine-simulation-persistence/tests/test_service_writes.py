"""The simulation service's role writes its tables in the Public API's database.

These tests run against a database the Public API's own v2 migration chain
built, with the privileges its manifest declares granted
(``PUBLIC_API_SCHEMA_DATABASE_URL``; see ``conftest``). Each test works in
one transaction and rolls it back, so the database stays as the chain built
it.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from conftest import (
    act_as_the_service,
    connection_as_the_service,
    rolled_back_transaction,
)
from policyengine_simulation_persistence import (
    HouseholdJobStatus,
    ReportRunStatus,
    ReportRunTrigger,
    SimulationStatus,
    SimulationType,
    budget_window_runs,
    household_jobs,
    missing_table_privileges,
    report_runs,
    reports,
    simulation_runs,
    simulations,
)
from sqlalchemy import func, insert, select, text

if TYPE_CHECKING:
    from sqlalchemy import Connection

pytestmark = pytest.mark.public_api_schema

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


def _insert_public_api_rows(connection: Connection) -> PublicApiRows:
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


def _insert_economy_simulation(
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


def _insert_simulation_run(
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


def test_the_service_role_holds_every_privilege_the_service_needs(
    public_api_database_url,
) -> None:
    with connection_as_the_service(public_api_database_url) as connection:
        assert missing_table_privileges(connection) == []


def test_the_service_role_creates_and_reads_a_row_in_every_table_it_writes(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        public_api_rows = _insert_public_api_rows(connection)
        act_as_the_service(connection)
        baseline_simulation_id = _insert_economy_simulation(connection, public_api_rows)
        reform_simulation_id = _insert_economy_simulation(connection, public_api_rows)
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
        baseline_run_id = _insert_simulation_run(
            connection, baseline_simulation_id, public_api_rows
        )
        reform_run_id = _insert_simulation_run(
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
        connection.execute(
            insert(household_jobs).values(
                country="us",
                request_data={},
                status=HouseholdJobStatus.PENDING,
                household_id=public_api_rows.household_id,
                year=YEAR,
                tax_benefit_model_version_id=public_api_rows.model_version_id,
                country_package_version=COUNTRY_PACKAGE_VERSION,
                policyengine_version=POLICYENGINE_VERSION,
            )
        )

        compared_runs = connection.execute(
            select(
                report_runs.c.baseline_simulation_run_id,
                report_runs.c.reform_simulation_run_id,
                report_runs.c.budget_window_run_id,
            ).where(report_runs.c.id == report_run_id)
        ).one()
        assert tuple(compared_runs) == (
            baseline_run_id,
            reform_run_id,
            budget_window_run_id,
        )
        for table in (
            simulations,
            reports,
            simulation_runs,
            budget_window_runs,
            report_runs,
            household_jobs,
        ):
            assert (
                connection.execute(select(func.count()).select_from(table)).scalar_one()
                >= 1
            ), table.name
