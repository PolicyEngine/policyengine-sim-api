"""Unit tests for the checks behind sim.persistence.orm-only."""

from __future__ import annotations

from textwrap import dedent

import pytest
from orm_access_support import (
    CORE_CONNECTION,
    CORE_TABLE_ACCESS,
    CORE_TABLE_DECLARATION,
    RAW_SQL,
    SQLMODEL_IMPORT,
    orm_access_findings,
)

STORE = "libs/example/src/example/store.py"
TABLES = "libs/example/src/example/tables.py"
ENGINE = "libs/example/src/example/engine.py"
ORM_BASE = "libs/example/src/example/orm_base.py"
GATEWAY = "projects/gateway/src/gateway/regions.py"

# Core tables under generic names, as the persistence library declares them.
CORE_TABLES_MODULE = """
from sqlalchemy import Column, MetaData, String, Table

metadata = MetaData()
household_jobs = Table("household_jobs", metadata, Column("id", String))
regions = Table("regions", metadata, Column("id", String))
reports = Table("reports", metadata, Column("id", String))
"""


def _checks(sources: dict[str, str], file: str) -> list[tuple[str, str]]:
    """Return the (check, source) findings in ``file``, with the tables module."""
    findings = orm_access_findings(
        {
            TABLES: dedent(CORE_TABLES_MODULE),
            **{name: dedent(source) for name, source in sources.items()},
        }
    )
    return [
        (finding.check, finding.source) for finding in findings if finding.file == file
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "select(func.count().label('jobs'))",
        "select(cast(HouseholdJob.year, String).label('year'))",
        "select(exists().where(HouseholdJob.id == job_id))",
        "select(*columns)",
        "select(pending.c.id).where(pending.c.id == job_id)",
        "select(func.count()).select_from(HouseholdJob).join(Policy)",
        "insert(HouseholdJob).values(id=job_id).on_conflict_do_nothing()",
        "select(reports.c.id)",
    ],
)
def test_orm_statements_are_accepted(statement):
    store = f"""
    from sqlalchemy import String, cast, exists, func, insert, select

    pending = select(HouseholdJob.id).subquery()
    reports = select(Report.id).where(Report.year == 2026).subquery()
    statement = {statement}
    """

    assert _checks({STORE: store}, STORE) == []


def test_names_shared_with_core_tables_elsewhere_are_not_core_tables():
    gateway = """
    import pyarrow as pa

    def describe(regions: list[str], households, frame: pa.Table) -> str:
        return ", ".join(regions) + str(households.columns) + str(frame.columns)
    """

    assert _checks({GATEWAY: gateway}, GATEWAY) == []


def test_sql_text_is_accepted_as_a_server_default_or_index_condition():
    store = """
    from sqlalchemy import Index, text
    from sqlalchemy.orm import mapped_column

    created_at = mapped_column(server_default=text("now()"))
    index = Index("ix_pending", "status", postgresql_where=text("status = 'pending'"))
    """

    assert _checks({STORE: store}, STORE) == []


def test_names_from_other_packages_are_not_sqlalchemy():
    store = """
    from textwrap import dedent as text

    settings.update(region="uk")
    text("SELECT 1")
    """

    assert _checks({STORE: store}, STORE) == []


def test_metadata_is_accepted_only_in_a_declarative_base_module():
    source = """
    from sqlalchemy import MetaData

    metadata = MetaData(schema="public")
    """

    assert _checks({ORM_BASE: source}, ORM_BASE) == []
    assert _checks({STORE: source}, STORE) == [
        (CORE_TABLE_DECLARATION, "MetaData(...)")
    ]


def test_lightweight_tables_and_columns_are_core_declarations():
    store = """
    import sqlalchemy as sa

    pg_class = sa.table("pg_class", sa.column("relname"))
    """

    assert _checks({STORE: store}, STORE) == [
        (CORE_TABLE_DECLARATION, "sa.table('pg_class', ...)"),
        (CORE_TABLE_DECLARATION, "sa.column('relname')"),
    ]


def test_every_reference_to_an_imported_core_table_is_reported():
    store = """
    from example.tables import household_jobs as jobs

    statement = jobs.insert()
    previous = jobs.alias("previous")
    condition = previous.c.id == job_id
    rows = session.query(jobs)
    """

    assert _checks({STORE: store}, STORE) == [
        (CORE_TABLE_ACCESS, "jobs"),
        (CORE_TABLE_ACCESS, "jobs"),
        (CORE_TABLE_ACCESS, "previous"),
        (CORE_TABLE_ACCESS, "jobs"),
    ]


def test_core_tables_reached_through_relative_imports_and_modules_are_reported():
    store = """
    from . import tables
    from .tables import regions

    condition = regions.c.id == tables.household_jobs.c.id
    """

    assert _checks({STORE: store}, STORE) == [
        (CORE_TABLE_ACCESS, "regions"),
        (CORE_TABLE_ACCESS, "tables.household_jobs"),
    ]


def test_a_parameter_annotated_as_a_sqlalchemy_table_is_a_core_table():
    store = """
    from sqlalchemy import Table

    def identifiers(table: Table):
        return table.c.id
    """

    assert _checks({STORE: store}, STORE) == [(CORE_TABLE_ACCESS, "table")]


@pytest.mark.parametrize(
    "expression",
    [
        "Base.metadata.tables['household_jobs']",
        "schema.tables['household_jobs']",
        "schema.sorted_tables",
        "HouseholdJob.__mapper__.local_table",
        "HouseholdJob.__mapper__.tables",
        "inspect(HouseholdJob).persist_selectable",
        "inspect(HouseholdJob).selectable",
        "HouseholdJob.__table__",
    ],
)
def test_reaching_a_mapped_class_or_metadata_table_is_core_table_access(expression):
    store = f"""
    from sqlalchemy import MetaData, inspect

    schema = MetaData()
    table = {expression}
    """

    assert [
        check
        for check, _ in _checks({STORE: store}, STORE)
        if check != CORE_TABLE_DECLARATION
    ] == [CORE_TABLE_ACCESS]


def test_engines_are_held_only_by_a_library_engine_module():
    source = """
    from sqlalchemy import Engine, create_engine
    from sqlalchemy.orm import sessionmaker
    """

    assert _checks({ENGINE: source}, ENGINE) == []
    assert _checks({STORE: source}, STORE) == [
        (CORE_CONNECTION, "sqlalchemy.Engine"),
        (CORE_CONNECTION, "sqlalchemy.create_engine"),
    ]


def test_an_engine_module_opens_sessions_not_connections():
    engine_module = """
    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection

    def bounded_transaction(engine: Engine):
        with engine.begin() as connection:
            yield connection

    def store_transaction(store):
        return store._engine.begin()

    def session_transaction(session_factory):
        return session_factory.begin()
    """

    assert _checks({ENGINE: engine_module}, ENGINE) == [
        (CORE_CONNECTION, "sqlalchemy.engine.Connection"),
        (CORE_CONNECTION, "engine.begin()"),
        (CORE_CONNECTION, "store._engine.begin()"),
    ]


@pytest.mark.parametrize(
    "call",
    [
        "engine.connect()",
        "session.connection()",
        "session.get_bind()",
        "engine.raw_connection()",
    ],
)
def test_opening_a_connection_is_reported_in_every_module(call):
    assert _checks({ENGINE: f"value = {call}"}, ENGINE) == [(CORE_CONNECTION, call)]


@pytest.mark.parametrize(
    ("source", "summary"),
    [
        ("sa.text('SELECT 1')", "sa.text('SELECT 1')"),
        ("sa.literal_column('count(*)')", "sa.literal_column('count(*)')"),
        ("session.exec_driver_sql('SELECT 1')", "session.exec_driver_sql('SELECT 1')"),
        ("driver_connection.cursor()", "driver_connection.cursor()"),
    ],
)
def test_raw_sql_is_reported(source, summary):
    store = f"""
    import sqlalchemy as sa

    statement = {source}
    """

    assert _checks({STORE: store}, STORE) == [(RAW_SQL, summary)]


@pytest.mark.parametrize(
    "statement", ["from sqlmodel import Session", "import sqlmodel"]
)
def test_sqlmodel_imports_are_reported(statement):
    assert _checks({STORE: statement}, STORE) == [(SQLMODEL_IMPORT, "sqlmodel")]
