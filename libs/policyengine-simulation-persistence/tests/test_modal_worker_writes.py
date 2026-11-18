"""The Modal worker role writes its runs and results in the Public API's database.

The v2 Modal coordinators, workers, household functions and stuck-run sweep
connect as ``policyengine_v2_modal_worker`` and write through this library's
models. These tests run against a database the Public API's own v2 migration
chain built, with the privileges its manifest declares granted
(``PUBLIC_API_SCHEMA_DATABASE_URL``; see ``conftest``). Each works in one
transaction and rolls it back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from conftest import (
    act_as_the_modal_worker,
    act_as_the_service,
    in_session,
    rolled_back_transaction,
)
from policyengine_simulation_persistence import (
    MODAL_WORKER_TABLE_PRIVILEGES,
    MODAL_WORKER_UPDATABLE_COLUMNS,
    BudgetWindowRun,
    HouseholdJob,
    Poverty,
    ReportRun,
    SimulationRun,
    missing_table_privileges,
    result_metadata,
)
from service_rows import ServiceRows, insert_public_api_rows, insert_service_rows
from sqlalchemy import Enum, Float, Integer, String, func, insert, select, text, update
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from sqlalchemy import Connection, Table

pytestmark = pytest.mark.public_api_schema

NOW = datetime(2026, 11, 16, tzinfo=UTC)
MODAL_CALL_ID = "fc-modal-worker-test"


def test_the_modal_worker_role_holds_every_privilege_the_modal_calls_need(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        act_as_the_modal_worker(connection)

        assert (
            in_session(
                connection,
                missing_table_privileges,
                table_privileges=MODAL_WORKER_TABLE_PRIVILEGES,
                updatable_columns=MODAL_WORKER_UPDATABLE_COLUMNS,
            )
            == []
        )


def _run_state(service_rows: ServiceRows) -> dict[Table, tuple[Any, dict[str, Any]]]:
    """Return, by run table, a run's ID and a value for every column the role updates.

    A Python ``None`` in a JSON result is written as one, so the test can read
    back that it is stored as SQL NULL.
    """
    finished = {
        "modal_call_id": MODAL_CALL_ID,
        "started_at": NOW,
        "completed_at": NOW,
        "updated_at": NOW,
    }
    return {
        ReportRun.__table__: (
            service_rows.report_run_id,
            {
                **finished,
                "status": "succeeded",
                "error_message": None,
                "baseline_cliff_gap": 1.0,
                "baseline_cliff_share": 0.1,
                "reform_cliff_gap": 2.0,
                "reform_cliff_share": 0.2,
                "spm_provenance": None,
                "data_version": "test-data-version",
            },
        ),
        SimulationRun.__table__: (
            service_rows.baseline_run_id,
            {
                **finished,
                "status": "succeeded",
                "error_message": None,
                "output_uri": "gs://test-bucket/simulation-run.parquet",
                "output_sha256": "0" * 64,
                "household_result": None,
            },
        ),
        HouseholdJob.__table__: (
            service_rows.household_job_id,
            {
                **finished,
                "status": "failed",
                "error_message": "The calculation failed.",
                "result": None,
            },
        ),
        BudgetWindowRun.__table__: (
            service_rows.budget_window_run_id,
            {
                "status": "running",
                "error_message": None,
                "started_at": NOW,
                "completed_at": None,
                "updated_at": NOW,
            },
        ),
    }


def _result_row(table: Table, service_rows: ServiceRows) -> dict[str, Any]:
    """Return a row with a value for each of ``table``'s required columns.

    A result of a report run names it and its two simulation runs; a result
    of one simulation run names the baseline's.
    """
    keys = {
        "report_run_id": service_rows.report_run_id,
        "baseline_simulation_run_id": service_rows.baseline_run_id,
        "reform_simulation_run_id": service_rows.reform_run_id,
        "simulation_run_id": service_rows.baseline_run_id,
    }
    row: dict[str, Any] = {"id": uuid4()}
    for column in table.columns:
        if column.name in row or column.server_default is not None:
            continue
        if column.name in keys:
            row[column.name] = keys[column.name]
        elif column.nullable:
            continue
        elif isinstance(column.type, Enum):
            row[column.name] = column.type.enums[0]
        elif isinstance(column.type, String):
            row[column.name] = f"test-{column.name}"[: column.type.length]
        elif isinstance(column.type, Integer):
            # A decile within every table's checks: 1, of 1 quantile.
            row[column.name] = 1
        elif isinstance(column.type, Float):
            row[column.name] = 0.5
        else:
            msg = f"no test value for {table.name}.{column.name}"
            raise AssertionError(msg)
    return row


def _stored_as_sql_null(connection: Connection, table: Table, column_name: str) -> int:
    return connection.execute(
        select(func.count())
        .select_from(table)
        .where(table.c[column_name].is_(None), table.c.modal_call_id == MODAL_CALL_ID)
    ).scalar_one()


def test_the_modal_worker_role_inserts_a_result_through_its_class(
    public_api_database_url,
) -> None:
    """A session's insert reads the row's server defaults back with ``RETURNING``."""
    with rolled_back_transaction(public_api_database_url) as connection:
        public_api_rows = insert_public_api_rows(connection)
        act_as_the_service(connection)
        service_rows = insert_service_rows(connection, public_api_rows)
        act_as_the_modal_worker(connection)
        result = Poverty(
            simulation_run_id=service_rows.baseline_run_id,
            poverty_type="spm",
            entity="person",
        )

        with Session(connection, join_transaction_mode="create_savepoint") as session:
            session.add(result)
            session.flush()
            created_at = result.created_at

    assert created_at is not None


def test_the_modal_worker_role_records_its_runs_and_inserts_every_result(
    public_api_database_url,
) -> None:
    with rolled_back_transaction(public_api_database_url) as connection:
        public_api_rows = insert_public_api_rows(connection)
        act_as_the_service(connection)
        service_rows = insert_service_rows(connection, public_api_rows)
        act_as_the_modal_worker(connection)

        for table, (run_id, values) in _run_state(service_rows).items():
            assert set(values) == MODAL_WORKER_UPDATABLE_COLUMNS[table.name], table
            updated = connection.execute(
                update(table).where(table.c.id == run_id).values(**values)
            )
            assert updated.rowcount == 1, table.name
        for table in result_metadata.tables.values():
            connection.execute(insert(table).values(_result_row(table, service_rows)))

        connection.execute(text("RESET ROLE"))
        for table in result_metadata.tables.values():
            stored = connection.execute(
                select(func.count()).select_from(table)
            ).scalar_one()
            assert stored >= 1, table.name
        assert (
            _stored_as_sql_null(connection, ReportRun.__table__, "spm_provenance") == 1
        )
        assert (
            _stored_as_sql_null(connection, SimulationRun.__table__, "household_result")
            == 1
        )
        assert _stored_as_sql_null(connection, HouseholdJob.__table__, "result") == 1
