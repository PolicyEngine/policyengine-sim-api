"""The simulation service's role writes its tables in the Public API's database.

These tests run against a database the Public API's own v2 migration chain
built, with the privileges its manifest declares granted
(``PUBLIC_API_SCHEMA_DATABASE_URL``; see ``conftest``). Each test works in
one transaction and rolls it back, so the database stays as the chain built
it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import (
    act_as_the_service,
    connection_as_the_service,
    in_session,
    rolled_back_transaction,
)
from policyengine_simulation_persistence import (
    BudgetWindowRun,
    CliffImpact,
    HouseholdJob,
    Report,
    ReportRun,
    Simulation,
    SimulationRun,
    missing_table_privileges,
)
from service_rows import insert_public_api_rows, insert_service_rows
from sqlalchemy import func, insert, select

pytestmark = pytest.mark.public_api_schema


def test_the_service_role_holds_every_privilege_the_service_needs(
    public_api_database_url,
) -> None:
    with connection_as_the_service(public_api_database_url) as connection:
        assert in_session(connection, missing_table_privileges) == []


def test_the_service_role_creates_and_reads_a_row_in_every_table_it_writes(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        public_api_rows = insert_public_api_rows(connection)
        act_as_the_service(connection)
        service_rows = insert_service_rows(connection, public_api_rows)
        # A cliff calculation of a simulation run, which the service starts.
        connection.execute(
            insert(CliffImpact.__table__).values(
                id=uuid4(),
                simulation_run_id=service_rows.baseline_run_id,
                status="pending",
            )
        )

        compared_runs = connection.execute(
            select(
                ReportRun.baseline_simulation_run_id,
                ReportRun.reform_simulation_run_id,
                ReportRun.budget_window_run_id,
            ).where(ReportRun.id == service_rows.report_run_id)
        ).one()
        assert tuple(compared_runs) == (
            service_rows.baseline_run_id,
            service_rows.reform_run_id,
            service_rows.budget_window_run_id,
        )
        for table in (
            Simulation.__table__,
            Report.__table__,
            SimulationRun.__table__,
            BudgetWindowRun.__table__,
            ReportRun.__table__,
            HouseholdJob.__table__,
            CliffImpact.__table__,
        ):
            assert (
                connection.execute(select(func.count()).select_from(table)).scalar_one()
                >= 1
            ), table.name
