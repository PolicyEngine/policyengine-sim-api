"""The privileges the simulation service's role needs on the Public API's tables."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import SERVICE_TEST_ROLE, simulation_engine
from policyengine_simulation_persistence import (
    MODAL_WORKER_TABLE_PRIVILEGES,
    MODAL_WORKER_UPDATABLE_COLUMNS,
    SERVICE_TABLE_PRIVILEGES,
    SERVICE_UPDATABLE_COLUMNS,
    create_session_factory,
    missing_table_privileges,
    read_missing_table_privileges,
    result_metadata,
    service_metadata,
)
from policyengine_simulation_persistence import access as access_module
from policyengine_simulation_persistence.catalog_tables import (
    PgAttribute,
    PgClass,
    PgNamespace,
)
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

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
    # Each simulation run's cliff calculations, which the service starts.
    "cliff_impacts",
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
    "dynamic_parameters",
    "dynamic_parameter_values",
)
# The run state the service records for a run whose Modal call it starts.
CALL_START_COLUMNS = frozenset(
    {"status", "modal_call_id", "error_message", "completed_at", "updated_at"}
)
# A budget-window run has no Modal call of its own: the service fails it when
# Modal refuses one of the first years it starts.
WINDOW_FAILURE_COLUMNS = frozenset(
    {"status", "error_message", "completed_at", "updated_at"}
)


def test_the_service_may_create_and_read_rows_in_every_table_it_writes() -> None:
    for table_name in WRITTEN_TABLES:
        assert SERVICE_TABLE_PRIVILEGES[table_name] == frozenset({"SELECT", "INSERT"})


def test_the_service_only_reads_the_rows_a_run_needs() -> None:
    for table_name in READ_TABLES:
        assert SERVICE_TABLE_PRIVILEGES[table_name] == frozenset({"SELECT"})
    assert set(SERVICE_TABLE_PRIVILEGES) == {*WRITTEN_TABLES, *READ_TABLES}


def test_the_service_updates_only_the_call_state_of_each_run() -> None:
    """It records a Modal call's ID, or fails a run Modal refuses, and no more.

    It fails a budget window when Modal refuses one of its first years.
    """
    assert dict(SERVICE_UPDATABLE_COLUMNS) == {
        "simulation_runs": CALL_START_COLUMNS,
        "report_runs": CALL_START_COLUMNS,
        "household_jobs": CALL_START_COLUMNS,
        "budget_window_runs": WINDOW_FAILURE_COLUMNS,
        "cliff_impacts": CALL_START_COLUMNS,
    }


def test_every_column_the_service_updates_is_modelled() -> None:
    tables = {table.name: table for table in service_metadata.tables.values()}

    for table_name, column_names in SERVICE_UPDATABLE_COLUMNS.items():
        assert column_names <= set(tables[table_name].columns.keys()), table_name


# The run tables whose status, call and results the Modal worker role records.
MODAL_WORKER_RUN_TABLES = (
    "report_runs",
    "simulation_runs",
    "household_jobs",
    "budget_window_runs",
    "cliff_impacts",
)
# A run's state, which the Modal call holding it records.
RUN_STATE_COLUMNS = frozenset(
    {
        "status",
        "modal_call_id",
        "error_message",
        "started_at",
        "completed_at",
        "updated_at",
    }
)


def test_the_modal_worker_role_reads_its_runs_and_inserts_their_results() -> None:
    result_tables = {table.name for table in result_metadata.tables.values()}

    assert dict(MODAL_WORKER_TABLE_PRIVILEGES) == {
        **dict.fromkeys(MODAL_WORKER_RUN_TABLES, frozenset({"SELECT"})),
        # A session's insert also reads the row's server defaults back.
        **dict.fromkeys(result_tables, frozenset({"SELECT", "INSERT"})),
    }


def test_the_modal_worker_role_updates_its_runs_state_and_results() -> None:
    """A call records its run's state and the results the run row holds.

    A budget-window run has no call of its own: the years' coordinators record
    its status, failure and times.
    """
    assert dict(MODAL_WORKER_UPDATABLE_COLUMNS) == {
        "report_runs": RUN_STATE_COLUMNS | {"spm_provenance", "data_version"},
        "simulation_runs": RUN_STATE_COLUMNS
        | {"output_uri", "output_sha256", "household_result"},
        "household_jobs": RUN_STATE_COLUMNS | {"result"},
        "budget_window_runs": RUN_STATE_COLUMNS - {"modal_call_id"},
        "cliff_impacts": RUN_STATE_COLUMNS | {"cliff_gap", "cliff_share"},
    }


def test_every_column_the_modal_worker_role_updates_is_modelled() -> None:
    tables = {table.name: table for table in service_metadata.tables.values()}

    for table_name, column_names in MODAL_WORKER_UPDATABLE_COLUMNS.items():
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
    engine = simulation_engine(database.service_url)
    try:
        with create_session_factory(engine).begin() as session:
            return missing_table_privileges(session)
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
def test_the_check_names_what_a_role_lacks_of_another_roles_lists(
    disposable_database,
):
    """Given the Modal worker role's lists, the check asks for them instead."""
    _grant(
        disposable_database,
        [
            "CREATE TABLE public.report_runs (id uuid PRIMARY KEY, "
            + ", ".join(
                f"{column_name} text" for column_name in sorted(RUN_STATE_COLUMNS)
            )
            + ")",
            "CREATE TABLE public.poverty (id uuid PRIMARY KEY)",
            f"GRANT SELECT ON public.report_runs TO {SERVICE_TEST_ROLE}",
            "GRANT UPDATE (status, modal_call_id) ON public.report_runs "
            f"TO {SERVICE_TEST_ROLE}",
        ],
    )
    engine = simulation_engine(disposable_database.service_url)
    try:
        with create_session_factory(engine).begin() as session:
            missing = missing_table_privileges(
                session,
                table_privileges={
                    "report_runs": frozenset({"SELECT"}),
                    "poverty": frozenset({"INSERT"}),
                },
                updatable_columns={"report_runs": frozenset({"status", "started_at"})},
            )
    finally:
        engine.dispose()

    assert missing == ["INSERT on poverty", "UPDATE (started_at) on report_runs"]


@pytest.mark.postgres
def test_a_partitioned_table_counts_as_the_table(disposable_database):
    """API v1 could partition a table; its grants still count."""
    other_tables = [name for name in SERVICE_TABLE_PRIVILEGES if name != "reports"]
    _create_tables(disposable_database, other_tables)
    _grant(
        disposable_database,
        ["CREATE TABLE public.reports (id uuid) PARTITION BY HASH (id)"],
    )
    _grant_every_required_privilege(disposable_database)

    assert _missing_privileges_as_the_service(disposable_database) == []


@pytest.mark.postgres
def test_a_view_of_a_required_tables_name_doesnt_count(disposable_database):
    """Grants on a view say nothing about the table the role needs."""
    other_tables = [name for name in SERVICE_TABLE_PRIVILEGES if name != "reports"]
    _create_tables(disposable_database, other_tables)
    _grant(
        disposable_database,
        ["CREATE VIEW public.reports AS SELECT gen_random_uuid() AS id"],
    )
    _grant_every_required_privilege(disposable_database)

    assert _missing_privileges_as_the_service(disposable_database) == [
        "INSERT on reports",
        "SELECT on reports",
    ]


@pytest.mark.postgres
def test_a_dropped_column_lacks_its_privilege(disposable_database):
    _create_tables(disposable_database, SERVICE_TABLE_PRIVILEGES)
    _grant_every_required_privilege(disposable_database)
    _grant(
        disposable_database,
        ["ALTER TABLE public.report_runs DROP COLUMN modal_call_id"],
    )

    assert _missing_privileges_as_the_service(disposable_database) == [
        "UPDATE (modal_call_id) on report_runs",
    ]


@pytest.mark.postgres
def test_a_system_column_isnt_a_column_the_role_can_update(disposable_database):
    """PostgreSQL numbers system columns below zero; they can't be granted."""
    _grant(
        disposable_database,
        [
            "CREATE TABLE public.report_runs (id uuid PRIMARY KEY)",
            f"GRANT UPDATE ON public.report_runs TO {SERVICE_TEST_ROLE}",
        ],
    )
    engine = simulation_engine(disposable_database.service_url)
    try:
        with create_session_factory(engine).begin() as session:
            missing = missing_table_privileges(
                session,
                table_privileges={"report_runs": frozenset({"UPDATE"})},
                updatable_columns={"report_runs": frozenset({"id", "xmin"})},
            )
    finally:
        engine.dispose()

    assert missing == ["UPDATE (xmin) on report_runs"]


@pytest.mark.postgres
def test_an_empty_list_requires_nothing(disposable_database):
    engine = simulation_engine(disposable_database.service_url)
    try:
        with create_session_factory(engine).begin() as session:
            missing = missing_table_privileges(
                session, table_privileges={}, updatable_columns={}
            )
    finally:
        engine.dispose()

    assert missing == []


@pytest.mark.postgres
def test_the_catalog_classes_read_postgresqls_catalog(disposable_database):
    """Each mapped column of the catalog classes exists under its own name."""
    _grant(
        disposable_database,
        ["CREATE TABLE public.reports (id uuid PRIMARY KEY, label text)"],
    )
    engine = simulation_engine(disposable_database.service_url)
    try:
        with create_session_factory(engine).begin() as session:
            table_row = session.execute(
                select(PgClass.relname, PgClass.relkind, PgNamespace.nspname)
                .join(PgNamespace, PgNamespace.oid == PgClass.relnamespace)
                .where(PgClass.relname == "reports")
            ).one()
            column_rows = session.execute(
                select(PgAttribute.attname, PgAttribute.attisdropped)
                .join(PgClass, PgClass.oid == PgAttribute.attrelid)
                .where(PgClass.relname == "reports", PgAttribute.attnum > 0)
                .order_by(PgAttribute.attnum)
            ).all()
    finally:
        engine.dispose()

    assert tuple(table_row) == ("reports", "r", "public")
    assert [tuple(row) for row in column_rows] == [("id", False), ("label", False)]


def test_the_catalog_classes_name_postgresqls_catalog_schema() -> None:
    for model, table_name in (
        (PgClass, "pg_class"),
        (PgNamespace, "pg_namespace"),
        (PgAttribute, "pg_attribute"),
    ):
        assert model.__table__.fullname == f"pg_catalog.{table_name}"


def _put_a_decoy_schema_first(database):
    """Create schema ``decoy``, usable by the role and first on its search_path."""
    _grant(
        database,
        [
            "CREATE SCHEMA decoy",
            f"GRANT USAGE ON SCHEMA decoy TO {SERVICE_TEST_ROLE}",
            f"ALTER ROLE {SERVICE_TEST_ROLE} IN DATABASE "
            f'"{database.name}" SET search_path = decoy, public',
        ],
    )


@pytest.mark.postgres
def test_only_the_public_schema_counts(disposable_database):
    _put_a_decoy_schema_first(disposable_database)
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


@pytest.mark.postgres
def test_only_the_public_schemas_columns_count(disposable_database):
    """A column grant on a table of the same name in another schema doesn't count."""
    _put_a_decoy_schema_first(disposable_database)
    _create_tables(disposable_database, SERVICE_TABLE_PRIVILEGES)
    _create_tables(disposable_database, ["report_runs"], schema="decoy")
    _grant_every_required_privilege(
        disposable_database, except_for={("UPDATE", "report_runs", "modal_call_id")}
    )
    _grant(
        disposable_database,
        [f"GRANT UPDATE (modal_call_id) ON decoy.report_runs TO {SERVICE_TEST_ROLE}"],
    )

    assert _missing_privileges_as_the_service(disposable_database) == [
        "UPDATE (modal_call_id) on report_runs",
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
            create_session_factory(engine), statement_timeout_milliseconds=2000
        )
        timeout_afterwards = _statement_timeout(engine)
    finally:
        engine.dispose()

    assert missing == []
    assert timeout_afterwards == "0"


@pytest.mark.postgres
def test_the_bounded_check_runs_every_statement_in_one_session(
    disposable_database, monkeypatch
):
    """The limit is set on the session the check itself then reads through."""
    sessions_used = []

    def record_session(session, **keywords):
        sessions_used.append(
            (
                session,
                session.execute(
                    select(func.current_setting("statement_timeout"))
                ).scalar_one(),
            )
        )
        return original_check(session, **keywords)

    original_check = access_module.missing_table_privileges
    monkeypatch.setattr(access_module, "missing_table_privileges", record_session)
    _create_tables(disposable_database, SERVICE_TABLE_PRIVILEGES)
    _grant_every_required_privilege(disposable_database)
    engine = _service_engine_with_one_connection(disposable_database)
    try:
        missing = read_missing_table_privileges(
            create_session_factory(engine), statement_timeout_milliseconds=2000
        )
    finally:
        engine.dispose()

    assert missing == []
    [(session, timeout)] = sessions_used
    assert isinstance(session, Session)
    assert timeout == "2s"


@pytest.mark.postgres
def test_the_database_ends_a_bounded_check_that_runs_too_long(
    disposable_database, monkeypatch
):
    def sleep_instead(session):
        session.execute(select(func.pg_sleep(2)))
        return []

    monkeypatch.setattr(access_module, "missing_table_privileges", sleep_instead)
    engine = _service_engine_with_one_connection(disposable_database)
    try:
        with pytest.raises(OperationalError, match="statement timeout"):
            read_missing_table_privileges(
                create_session_factory(engine), statement_timeout_milliseconds=100
            )
        timeout_afterwards = _statement_timeout(engine)
    finally:
        engine.dispose()

    assert timeout_afterwards == "0"
