"""The privileges the simulation service's role needs on the Public API's tables."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import SERVICE_TEST_ROLE
from policyengine_simulation_persistence import (
    SERVICE_TABLE_PRIVILEGES,
    SERVICE_UPDATABLE_COLUMNS,
    create_simulation_engine,
    missing_table_privileges,
    read_missing_table_privileges,
    service_metadata,
)
from policyengine_simulation_persistence import access as access_module
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

PROOF_FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "rules"
    / "fixtures"
    / "sim.persistence.missing-privileges-reported"
)
WRITTEN_TABLES = (
    "simulations",
    "reports",
    "simulation_runs",
    "report_runs",
    "budget_window_runs",
    "household_jobs",
)
READ_TABLES = (
    "policies",
    "households",
    "tax_benefit_models",
    "tax_benefit_model_versions",
    "regions",
    "datasets",
    "parameters",
    "parameter_values",
    "dynamics",
)
# The run state the service records for a run whose Modal call it starts.
CALL_START_COLUMNS = frozenset(
    {"status", "modal_call_id", "error_message", "completed_at", "updated_at"}
)


def test_the_service_may_create_and_read_rows_in_every_table_it_writes() -> None:
    for table_name in WRITTEN_TABLES:
        assert SERVICE_TABLE_PRIVILEGES[table_name] == frozenset({"SELECT", "INSERT"})


def test_the_service_only_reads_the_rows_a_run_needs() -> None:
    for table_name in READ_TABLES:
        assert SERVICE_TABLE_PRIVILEGES[table_name] == frozenset({"SELECT"})
    assert set(SERVICE_TABLE_PRIVILEGES) == {*WRITTEN_TABLES, *READ_TABLES}


def test_the_service_updates_only_the_call_state_of_each_run() -> None:
    """It records a Modal call's ID, or fails a run Modal refuses, and no more."""
    assert dict(SERVICE_UPDATABLE_COLUMNS) == {
        "simulation_runs": CALL_START_COLUMNS,
        "report_runs": CALL_START_COLUMNS,
        "household_jobs": CALL_START_COLUMNS,
    }


def test_every_column_the_service_updates_is_modelled() -> None:
    tables = {table.name: table for table in service_metadata.tables.values()}

    for table_name, column_names in SERVICE_UPDATABLE_COLUMNS.items():
        assert column_names <= set(tables[table_name].columns.keys()), table_name


def _required_grants() -> list[tuple[str, ...]]:
    """Return every grant the service needs, each a tuple.

    A table's is ``(privilege, table)``, a column's ``("UPDATE", table, column)``.
    """
    table_grants = [
        (privilege, table_name)
        for table_name, privileges in SERVICE_TABLE_PRIVILEGES.items()
        for privilege in sorted(privileges)
    ]
    column_grants = [
        ("UPDATE", table_name, column_name)
        for table_name, column_names in SERVICE_UPDATABLE_COLUMNS.items()
        for column_name in sorted(column_names)
    ]
    return [*table_grants, *column_grants]


def _grant_statement(grant: tuple[str, ...]) -> str:
    privilege, table_name, *column_names = grant
    columns = f" ({', '.join(column_names)})" if column_names else ""
    return f"GRANT {privilege}{columns} ON public.{table_name} TO {SERVICE_TEST_ROLE}"


def _superuser_engine(database):
    return create_engine(
        make_url(database.superuser_url).set(drivername="postgresql+psycopg"),
        isolation_level="AUTOCOMMIT",
    )


def _create_tables(database, table_names, *, schema="public", leave_out=()):
    """Create each table with an id and every column the service updates.

    ``leave_out`` holds ``(table, column)`` pairs to create without.
    """
    engine = _superuser_engine(database)
    try:
        with engine.connect() as connection:
            for table_name in table_names:
                columns = [
                    "id uuid PRIMARY KEY",
                    *(
                        f"{column_name} text"
                        for column_name in sorted(
                            SERVICE_UPDATABLE_COLUMNS.get(table_name, ())
                        )
                        if (table_name, column_name) not in leave_out
                    ),
                ]
                connection.execute(
                    text(f"CREATE TABLE {schema}.{table_name} ({', '.join(columns)})")
                )
    finally:
        engine.dispose()


def _grant(database, statements):
    engine = _superuser_engine(database)
    try:
        with engine.connect() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def _grant_every_required_privilege(database, *, except_for=()):
    """Grant what ``_required_grants`` lists, but for the grants in ``except_for``."""
    _grant(
        database,
        [
            _grant_statement(grant)
            for grant in _required_grants()
            if grant not in except_for
        ],
    )


def _missing_privileges_as_the_service(database):
    engine = create_simulation_engine(database.service_url)
    try:
        with engine.connect() as connection:
            return missing_table_privileges(connection)
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_a_role_with_every_required_privilege_misses_none(disposable_database):
    _create_tables(disposable_database, SERVICE_TABLE_PRIVILEGES)
    _grant_every_required_privilege(disposable_database)

    assert _missing_privileges_as_the_service(disposable_database) == []


@pytest.mark.postgres
def test_each_missing_privilege_is_named(disposable_database):
    _create_tables(disposable_database, SERVICE_TABLE_PRIVILEGES)
    _grant_every_required_privilege(
        disposable_database,
        except_for={("INSERT", "simulation_runs"), ("SELECT", "policies")},
    )

    assert _missing_privileges_as_the_service(disposable_database) == [
        "SELECT on policies",
        "INSERT on simulation_runs",
    ]


@pytest.mark.postgres
def test_a_missing_table_lacks_every_privilege(disposable_database):
    present_tables = [
        table_name
        for table_name in SERVICE_TABLE_PRIVILEGES
        if table_name != "household_jobs"
    ]
    _create_tables(disposable_database, present_tables)
    _grant(
        disposable_database,
        [
            _grant_statement(grant)
            for grant in _required_grants()
            if grant[1] != "household_jobs"
        ],
    )

    assert _missing_privileges_as_the_service(disposable_database) == [
        "INSERT on household_jobs",
        "SELECT on household_jobs",
        "UPDATE (completed_at) on household_jobs",
        "UPDATE (error_message) on household_jobs",
        "UPDATE (modal_call_id) on household_jobs",
        "UPDATE (status) on household_jobs",
        "UPDATE (updated_at) on household_jobs",
    ]


@pytest.mark.postgres
def test_each_missing_column_privilege_is_named(disposable_database):
    _create_tables(disposable_database, SERVICE_TABLE_PRIVILEGES)
    _grant_every_required_privilege(
        disposable_database,
        except_for={
            ("UPDATE", "report_runs", "modal_call_id"),
            ("UPDATE", "household_jobs", "status"),
        },
    )

    assert _missing_privileges_as_the_service(disposable_database) == [
        "UPDATE (status) on household_jobs",
        "UPDATE (modal_call_id) on report_runs",
    ]


@pytest.mark.postgres
def test_a_missing_column_lacks_its_privilege(disposable_database):
    """A column the table doesn't have can't be updated, so it is named."""
    _create_tables(
        disposable_database,
        SERVICE_TABLE_PRIVILEGES,
        leave_out={("report_runs", "modal_call_id")},
    )
    _grant_every_required_privilege(
        disposable_database, except_for={("UPDATE", "report_runs", "modal_call_id")}
    )

    assert _missing_privileges_as_the_service(disposable_database) == [
        "UPDATE (modal_call_id) on report_runs",
    ]


@pytest.mark.postgres
def test_only_the_public_schema_counts(disposable_database):
    _grant(
        disposable_database,
        [
            "CREATE SCHEMA decoy",
            f"GRANT USAGE ON SCHEMA decoy TO {SERVICE_TEST_ROLE}",
            f"ALTER ROLE {SERVICE_TEST_ROLE} IN DATABASE "
            f'"{disposable_database.name}" SET search_path = decoy, public',
        ],
    )
    _create_tables(disposable_database, SERVICE_TABLE_PRIVILEGES)
    _create_tables(disposable_database, ["reports"], schema="decoy")
    _grant_every_required_privilege(
        disposable_database, except_for={("INSERT", "reports")}
    )
    _grant(
        disposable_database,
        [f"GRANT SELECT, INSERT ON decoy.reports TO {SERVICE_TEST_ROLE}"],
    )

    assert _missing_privileges_as_the_service(disposable_database) == [
        "INSERT on reports",
    ]


def _granted_privileges(fixture: Path) -> list[tuple[str, ...]]:
    """Read ``<PRIVILEGE> <table> [<column>]`` lines, skipping comments."""
    lines = fixture.read_text(encoding="utf-8").splitlines()
    return [tuple(line.split()) for line in lines if line and not line.startswith("#")]


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("fixture_name", "expected_missing"),
    [
        (
            "bad_grants.txt",
            ["UPDATE (modal_call_id) on report_runs", "INSERT on simulation_runs"],
        ),
        ("good_grants.txt", []),
    ],
)
def test_rule_proof_missing_privileges_reported(
    disposable_database, fixture_name, expected_missing
):
    """Proof for rule sim.persistence.missing-privileges-reported.

    The bad grants leave out ``INSERT`` on ``simulation_runs`` and ``UPDATE``
    on ``report_runs.modal_call_id``; the good ones hold every privilege the
    service needs.
    """
    _create_tables(disposable_database, SERVICE_TABLE_PRIVILEGES)
    _grant(
        disposable_database,
        [
            _grant_statement(grant)
            for grant in _granted_privileges(PROOF_FIXTURES / fixture_name)
        ],
    )

    assert _missing_privileges_as_the_service(disposable_database) == expected_missing


@pytest.mark.postgres
def test_a_role_without_usage_on_the_schema_is_told_so(disposable_database):
    """Without USAGE on ``public``, the role can't use its table grants."""
    _create_tables(disposable_database, SERVICE_TABLE_PRIVILEGES)
    _grant_every_required_privilege(disposable_database)
    _grant(disposable_database, ["REVOKE USAGE ON SCHEMA public FROM PUBLIC"])

    assert _missing_privileges_as_the_service(disposable_database) == [
        "USAGE on schema public",
    ]


def _service_engine_with_one_connection(database):
    """Return an engine whose next connection is always the one it pooled."""
    return create_engine(
        make_url(database.service_url).set(drivername="postgresql+psycopg"),
        pool_size=1,
        max_overflow=0,
    )


def _statement_timeout(engine) -> str:
    with engine.connect() as connection:
        return connection.execute(text("SHOW statement_timeout")).scalar_one()


@pytest.mark.postgres
def test_the_bounded_check_reads_the_privileges_and_leaves_no_timeout_behind(
    disposable_database,
):
    _create_tables(disposable_database, SERVICE_TABLE_PRIVILEGES)
    _grant_every_required_privilege(disposable_database)
    engine = _service_engine_with_one_connection(disposable_database)
    try:
        missing = read_missing_table_privileges(
            engine, statement_timeout_milliseconds=2000
        )
        timeout_afterwards = _statement_timeout(engine)
    finally:
        engine.dispose()

    assert missing == []
    assert timeout_afterwards == "0"


@pytest.mark.postgres
def test_the_database_ends_a_bounded_check_that_runs_too_long(
    disposable_database, monkeypatch
):
    def sleep_instead(connection):
        connection.execute(select(func.pg_sleep(2)))
        return []

    monkeypatch.setattr(access_module, "missing_table_privileges", sleep_instead)
    engine = _service_engine_with_one_connection(disposable_database)
    try:
        with pytest.raises(OperationalError, match="statement timeout"):
            read_missing_table_privileges(engine, statement_timeout_milliseconds=100)
        timeout_afterwards = _statement_timeout(engine)
    finally:
        engine.dispose()

    assert timeout_afterwards == "0"
