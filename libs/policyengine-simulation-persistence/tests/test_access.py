"""The privileges the simulation service's role needs on the Public API's tables."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import SERVICE_TEST_ROLE
from policyengine_simulation_persistence import (
    SERVICE_TABLE_PRIVILEGES,
    create_simulation_engine,
    missing_table_privileges,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

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
    "report_run_simulation_runs",
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
)


def test_the_service_may_create_and_read_rows_in_every_table_it_writes() -> None:
    for table_name in WRITTEN_TABLES:
        assert SERVICE_TABLE_PRIVILEGES[table_name] == frozenset({"SELECT", "INSERT"})


def test_the_service_only_reads_the_rows_a_run_needs() -> None:
    for table_name in READ_TABLES:
        assert SERVICE_TABLE_PRIVILEGES[table_name] == frozenset({"SELECT"})
    assert set(SERVICE_TABLE_PRIVILEGES) == {*WRITTEN_TABLES, *READ_TABLES}


def _superuser_engine(database):
    return create_engine(
        make_url(database.superuser_url).set(drivername="postgresql+psycopg"),
        isolation_level="AUTOCOMMIT",
    )


def _create_tables(database, table_names, *, schema="public"):
    engine = _superuser_engine(database)
    try:
        with engine.connect() as connection:
            for table_name in table_names:
                connection.execute(
                    text(f"CREATE TABLE {schema}.{table_name} (id uuid PRIMARY KEY)")
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
    _grant(
        database,
        [
            f"GRANT {privilege} ON public.{table_name} TO {SERVICE_TEST_ROLE}"
            for table_name, privileges in SERVICE_TABLE_PRIVILEGES.items()
            for privilege in sorted(privileges)
            if (privilege, table_name) not in except_for
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
            f"GRANT {privilege} ON public.{table_name} TO {SERVICE_TEST_ROLE}"
            for table_name in present_tables
            for privilege in sorted(SERVICE_TABLE_PRIVILEGES[table_name])
        ],
    )

    assert _missing_privileges_as_the_service(disposable_database) == [
        "INSERT on household_jobs",
        "SELECT on household_jobs",
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


def _granted_privileges(fixture: Path) -> list[tuple[str, str]]:
    """Read ``<PRIVILEGE> <table>`` lines, skipping comments."""
    lines = fixture.read_text(encoding="utf-8").splitlines()
    return [
        (privilege, table_name)
        for privilege, table_name in (
            line.split() for line in lines if line and not line.startswith("#")
        )
    ]


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("fixture_name", "expected_missing"),
    [("bad_grants.txt", ["INSERT on simulation_runs"]), ("good_grants.txt", [])],
)
def test_rule_proof_missing_privileges_reported(
    disposable_database, fixture_name, expected_missing
):
    """Proof for rule sim.persistence.missing-privileges-reported.

    The bad grants leave out ``INSERT`` on ``simulation_runs``; the good ones
    hold every privilege the service needs.
    """
    _create_tables(disposable_database, SERVICE_TABLE_PRIVILEGES)
    _grant(
        disposable_database,
        [
            f"GRANT {privilege} ON public.{table_name} TO {SERVICE_TEST_ROLE}"
            for privilege, table_name in _granted_privileges(
                PROOF_FIXTURES / fixture_name
            )
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
