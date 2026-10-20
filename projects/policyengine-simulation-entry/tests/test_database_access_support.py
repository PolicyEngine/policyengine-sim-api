"""Check how database_access_support recognises database access and schema changes."""

from __future__ import annotations

import textwrap

import pytest
from database_access_support import (
    ALEMBIC_IMPORT,
    DDL_CONSTRUCT,
    DDL_TEXT,
    DRIVER_IMPORT,
    SCHEMA_CHANGE_CALL,
    database_access_findings,
)

RUNTIME_MODULE = "libs/example/src/example/store.py"
TABLES_MODULE = "libs/example/src/example/tables.py"
MIGRATION_MODULE = "libs/example/src/example/migrations/versions/0001_initial.py"
TABLES_SOURCE = """
from sqlalchemy import Column, MetaData, String, Table

metadata = MetaData()
comparison_reports = Table("reports", metadata, Column("id", String))
"""


def _checks(source: str, file: str = RUNTIME_MODULE) -> list[tuple[str, str]]:
    sources = {TABLES_MODULE: TABLES_SOURCE, file: textwrap.dedent(source)}
    return [
        (finding.check, finding.source)
        for finding in database_access_findings(sources)
        if finding.file == file
    ]


@pytest.mark.parametrize(
    ("import_statement", "expected_source"),
    [
        ("import psycopg", "import psycopg"),
        ("import psycopg2.extras", "import psycopg2.extras"),
        (
            "from psycopg.types.json import Jsonb",
            "from psycopg.types.json import Jsonb",
        ),
        ("import mysql.connector", "import mysql.connector"),
        ("from mysql import connector", "from mysql import connector"),
        ("import sqlite3 as local_database", "import sqlite3 as local_database"),
        ("from asyncpg import connect", "from asyncpg import connect"),
        ("import MySQLdb", "import MySQLdb"),
    ],
)
def test_driver_imports_are_findings(import_statement, expected_source) -> None:
    assert _checks(import_statement) == [(DRIVER_IMPORT, expected_source)]


@pytest.mark.parametrize(
    "import_statement",
    [
        "from sqlalchemy.dialects import sqlite",
        "from sqlalchemy.dialects.postgresql import insert",
        "import sqlalchemy.dialects.sqlite",
        "from . import sqlite3",
        "import mysqlclient_settings",
    ],
)
def test_sqlalchemy_and_unrelated_imports_are_not_findings(import_statement) -> None:
    assert _checks(import_statement) == []


def test_alembic_imports_only_inside_a_migrations_directory() -> None:
    migration = """
    from alembic import op

    def upgrade():
        op.execute("CREATE SCHEMA simulation")
    """

    assert _checks("from alembic import command") == [
        (ALEMBIC_IMPORT, "from alembic import command")
    ]
    assert _checks("import alembic.config") == [
        (ALEMBIC_IMPORT, "import alembic.config")
    ]
    assert _checks(migration, MIGRATION_MODULE) == []


def test_migrations_still_reach_the_database_through_sqlalchemy() -> None:
    assert _checks("import psycopg", MIGRATION_MODULE) == [
        (DRIVER_IMPORT, "import psycopg")
    ]


@pytest.mark.parametrize(
    ("statement", "expected_source"),
    [
        ("metadata.create_all(engine)", "metadata.create_all(engine)"),
        (
            "SQLModel.metadata.drop_all(bind=engine)",
            "SQLModel.metadata.drop_all(bind=engine)",
        ),
        ("comparison_reports.create(engine)", "comparison_reports.create(engine)"),
        (
            "tables.comparison_reports.drop(connection)",
            "tables.comparison_reports.drop(connection)",
        ),
        ("Report.__table__.create(engine)", "Report.__table__.create(engine)"),
        (
            "metadata.tables['reports'].drop(engine)",
            "metadata.tables['reports'].drop(engine)",
        ),
        (
            "Table('reports', metadata).create(engine)",
            "Table('reports', metadata).create(engine)",
        ),
        (
            "report_index.create(engine, checkfirst=True)",
            "report_index.create(engine, checkfirst=True)",
        ),
    ],
)
def test_schema_change_calls_are_findings(statement, expected_source) -> None:
    assert _checks(statement) == [(SCHEMA_CHANGE_CALL, expected_source)]


def test_a_parameter_annotated_as_a_table_is_a_table() -> None:
    source = """
    from sqlalchemy import Table

    def reset(table: Table, connection):
        table.drop(connection)
    """

    assert _checks(source) == [(SCHEMA_CHANGE_CALL, "table.drop(connection)")]


@pytest.mark.parametrize(
    "statement",
    [
        "frame.drop(columns=['household_weight'])",
        "frame.drop('household_weight', axis=1)",
        "client.create(name='report')",
        "modal.Dict.from_name('reports', create_if_missing=True)",
        "create_engine('postgresql+psycopg://user@host/database')",
    ],
)
def test_calls_on_other_objects_are_not_findings(statement) -> None:
    assert _checks(statement) == []


def test_ddl_text_in_literals_but_not_docstrings() -> None:
    source = '''
    """Never run CREATE TABLE here; policyengine-api owns the schema."""

    def create_reports(connection, name):
        """Do not DROP TABLE either."""
        connection.exec_driver_sql("create table reports (id text)")
        connection.exec_driver_sql(f"ALTER TYPE {name} ADD VALUE 'done'")
    '''

    assert _checks(source) == [
        (DDL_TEXT, "'create table reports (id text)'"),
        (DDL_TEXT, "'ALTER TYPE '"),
    ]


def test_sqlalchemy_ddl_constructs_are_findings() -> None:
    source = """
    import sqlalchemy as sa
    from sqlalchemy.schema import CreateTable

    def build(connection, table):
        connection.execute(CreateTable(table))
        connection.execute(sa.schema.DropTable(table))
    """

    assert _checks(source) == [
        (DDL_CONSTRUCT, "from sqlalchemy.schema import CreateTable"),
        (DDL_CONSTRUCT, "sa.schema.DropTable"),
    ]
