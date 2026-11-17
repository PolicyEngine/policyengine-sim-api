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
    STATEMENT_ON_CORE_TABLE,
    orm_access_findings,
)

STORE = "libs/example/src/example/store.py"
TABLES = "libs/example/src/example/tables.py"
ENGINE = "libs/example/src/example/engine.py"
ORM_BASE = "libs/example/src/example/orm_base.py"

CORE_TABLES_MODULE = """
from sqlalchemy import Column, MetaData, String, Table

metadata = MetaData()
household_jobs = Table("household_jobs", metadata, Column("id", String))
"""


def _checks(sources: dict[str, str]) -> list[tuple[str, str, str]]:
    findings = orm_access_findings(
        {file: dedent(source) for file, source in sources.items()}
    )
    return [(finding.file, finding.check, finding.source) for finding in findings]


@pytest.mark.parametrize(
    "statement",
    [
        "select(func.count().label('jobs'))",
        "select(cast(HouseholdJob.year, String).label('year'))",
        "select(exists().where(HouseholdJob.id == job_id))",
        "select(*columns)",
        "select(pending.c.id).where(pending.c.id == job_id)",
        "select(func.count()).select_from(HouseholdJob).join(Policy)",
    ],
)
def test_orm_statements_on_expressions_and_subqueries_are_accepted(statement):
    source = f"""
    from sqlalchemy import String, cast, exists, func, select

    pending = select(HouseholdJob.id).subquery()
    statement = {statement}
    """

    assert _checks({STORE: source}) == []


def test_sql_text_is_accepted_as_a_server_default_or_index_condition():
    source = """
    from sqlalchemy import Index, text
    from sqlalchemy.orm import mapped_column

    created_at = mapped_column(server_default=text("now()"))
    index = Index("ix_pending", "status", postgresql_where=text("status = 'pending'"))
    """

    assert _checks({STORE: source}) == []


def test_names_from_other_packages_are_not_sqlalchemy():
    source = """
    from textwrap import dedent as text

    settings.update(region="uk")
    frame.columns
    text("SELECT 1")
    """

    assert _checks({STORE: source}) == []


def test_metadata_is_accepted_only_in_a_declarative_base_module():
    source = """
    from sqlalchemy import MetaData

    metadata = MetaData(schema="public")
    """

    assert _checks({ORM_BASE: source}) == []
    assert _checks({STORE: source}) == [
        (STORE, CORE_TABLE_DECLARATION, "MetaData(...)")
    ]


def test_lightweight_tables_and_columns_are_core_declarations():
    source = """
    import sqlalchemy as sa

    pg_class = sa.table("pg_class", sa.column("relname"))
    """

    assert _checks({STORE: source}) == [
        (STORE, CORE_TABLE_DECLARATION, "sa.table('pg_class', ...)"),
        (STORE, CORE_TABLE_DECLARATION, "sa.column('relname')"),
    ]


def test_a_table_imported_under_another_name_is_still_a_core_table():
    store = """
    from sqlalchemy import insert

    from example.tables import household_jobs as jobs

    statement = insert(jobs).returning(jobs.c.id)
    joined = select_jobs.select_from(jobs)
    """

    assert [
        (check, source)
        for file, check, source in _checks({TABLES: CORE_TABLES_MODULE, STORE: store})
        if file == STORE
    ] == [
        (STATEMENT_ON_CORE_TABLE, "insert(jobs)"),
        (CORE_TABLE_ACCESS, "jobs.c"),
        (STATEMENT_ON_CORE_TABLE, "select_jobs.select_from(jobs)"),
    ]


@pytest.mark.parametrize(
    "expression",
    [
        "Base.metadata.tables['household_jobs']",
        "HouseholdJob.__mapper__.local_table",
        "inspect(HouseholdJob).persist_selectable",
        "HouseholdJob.__table__",
    ],
)
def test_reaching_a_mapped_class_table_is_core_table_access(expression):
    source = f"table = {expression}"

    assert [check for _, check, _ in _checks({STORE: source})] == [CORE_TABLE_ACCESS]


def test_engines_are_held_only_by_a_library_engine_module():
    source = """
    from sqlalchemy import Engine, create_engine
    from sqlalchemy.orm import sessionmaker
    """

    assert _checks({ENGINE: source}) == []
    assert _checks({STORE: source}) == [
        (STORE, CORE_CONNECTION, "sqlalchemy.Engine"),
        (STORE, CORE_CONNECTION, "sqlalchemy.create_engine"),
    ]


@pytest.mark.parametrize(
    "call", ["engine.connect()", "session.connection()", "session.get_bind()"]
)
def test_opening_a_connection_is_reported_in_every_module(call):
    assert _checks({ENGINE: f"value = {call}"}) == [(ENGINE, CORE_CONNECTION, call)]


def test_raw_sql_and_sqlmodel_are_reported():
    source = """
    import sqlalchemy as sa
    from sqlmodel import Session

    statement = sa.text("SELECT 1")
    """

    assert _checks({STORE: source}) == [
        (STORE, SQLMODEL_IMPORT, "sqlmodel"),
        (STORE, RAW_SQL, "sa.text('SELECT 1')"),
    ]
