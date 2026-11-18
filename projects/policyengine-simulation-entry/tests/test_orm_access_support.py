"""Unit tests for the checks behind sim.persistence.orm-only."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from textwrap import dedent

import pytest
from orm_access_support import (
    CORE_CONNECTION,
    CORE_TABLE_ACCESS,
    CORE_TABLE_DECLARATION,
    RAW_SQL,
    SQLMODEL_IMPORT,
    UNMAPPED_TABLE,
    orm_access_findings,
    unmapped_table_findings,
)

STORE = "libs/example/src/example/store.py"
TABLES = "libs/example/src/example/tables.py"
ENGINE = "libs/example/src/example/engine.py"
ORM_BASE = "libs/example/src/example/orm_base.py"
GATEWAY = "projects/gateway/src/gateway/regions.py"
PACKAGE = "libs/example/src/example/__init__.py"
MODELS = "libs/example/src/example/models.py"

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
    from sqlalchemy import CheckConstraint, Index, text
    from sqlalchemy.orm import mapped_column

    created_at = mapped_column(server_default=text("now()"))
    positive = CheckConstraint(text("key_version > 0"), name="ck_key_version")
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
        "class_mapper(HouseholdJob).tables",
        "inspect(HouseholdJob).mapper.tables",
        "job_mapper.tables",
        "held_mapper.tables",
        "inspected_mapper.tables",
        "HouseholdJob.id.table",
    ],
)
def test_reaching_a_mapped_class_or_metadata_table_is_core_table_access(expression):
    store = f"""
    from sqlalchemy import MetaData, inspect
    from sqlalchemy.orm import class_mapper

    schema = MetaData()
    job_mapper = inspect(HouseholdJob)
    held_mapper = HouseholdJob.__mapper__
    inspected_mapper = inspect(job).mapper
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
def test_opening_a_connection_is_reported_in_every_module_using_sqlalchemy(call):
    source = f"import sqlalchemy\n\nvalue = {call}"

    assert _checks({ENGINE: source}, ENGINE) == [(CORE_CONNECTION, call)]


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


def test_a_package_re_export_and_a_dotted_import_reach_core_tables():
    package = """
    from example import tables

    household_jobs = tables.household_jobs
    """
    store = """
    import example
    import example.tables

    jobs = example.household_jobs
    statement = jobs.update()
    condition = example.tables.regions.c.id == job_id
    """

    sources = {PACKAGE: package, STORE: store}
    assert _checks(sources, PACKAGE) == [(CORE_TABLE_ACCESS, "tables.household_jobs")]
    assert _checks(sources, STORE) == [
        (CORE_TABLE_ACCESS, "example.household_jobs"),
        (CORE_TABLE_ACCESS, "jobs"),
        (CORE_TABLE_ACCESS, "example.tables.regions"),
    ]


def test_star_imports_and_attribute_targets_carry_core_tables():
    store = """
    from example.tables import *

    class Store:
        def __init__(self):
            self.jobs = household_jobs

        def statement(self):
            return self.jobs.insert(), regions.c.id
    """

    assert _checks({STORE: store}, STORE) == [
        (CORE_TABLE_ACCESS, "household_jobs"),
        (CORE_TABLE_ACCESS, "self.jobs"),
        (CORE_TABLE_ACCESS, "regions"),
    ]


def test_a_variable_annotated_as_a_sqlalchemy_table_is_a_core_table():
    store = """
    from sqlalchemy import Table

    selected: Table = choose_table()
    condition = selected.c.id == job_id
    """

    assert _checks({STORE: store}, STORE) == [(CORE_TABLE_ACCESS, "selected")]


def test_two_files_with_one_module_name_are_refused():
    sources = {
        "projects/first/src/modal/app.py": "",
        "projects/second/src/modal/app.py": "",
    }

    with pytest.raises(ValueError, match=r"are both module modal\.app"):
        orm_access_findings(sources)


def test_begin_counts_as_opening_a_connection_only_on_an_engine():
    engine_module = """
    from sqlalchemy import create_engine

    primary = create_engine(url)
    first = primary.begin()
    second = policyengine_settings.begin()
    """

    assert _checks({ENGINE: engine_module}, ENGINE) == [
        (CORE_CONNECTION, "primary.begin()")
    ]


def test_async_engines_and_connections_are_reported():
    store = "from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine"

    assert _checks({STORE: store}, STORE) == [
        (CORE_CONNECTION, "sqlalchemy.ext.asyncio.AsyncConnection"),
        (CORE_CONNECTION, "sqlalchemy.ext.asyncio.AsyncEngine"),
    ]


@pytest.mark.parametrize(
    ("source", "check"),
    [
        ("sa.TableClause('household_jobs')", CORE_TABLE_DECLARATION),
        ("sa.ColumnClause('id')", CORE_TABLE_DECLARATION),
        ("sa.TextClause('SELECT 1')", RAW_SQL),
    ],
)
def test_core_clause_classes_are_reported(source, check):
    store = f"""
    import sqlalchemy as sa

    clause = {source}
    """

    assert [found for found, _ in _checks({STORE: store}, STORE)] == [check]


def _load_module(name: str, source: str, monkeypatch) -> types.ModuleType:
    module = types.ModuleType(name)
    # SQLAlchemy resolves a mapped class's annotations in its module.
    monkeypatch.setitem(sys.modules, name, module)
    exec(compile(dedent(source), name, "exec"), module.__dict__)
    return module


def test_the_runtime_check_accepts_tables_mapped_by_either_declarative_base(
    monkeypatch,
):
    mapped = _load_module(
        "orm_only_mapped_models",
        """
        from sqlalchemy import MetaData, Table
        from sqlalchemy.orm import (
            DeclarativeBase,
            DeclarativeBaseNoMeta,
            Mapped,
            mapped_column,
        )

        class Base(DeclarativeBase):
            pass

        class NoMetaBase(DeclarativeBaseNoMeta):
            metadata = MetaData()

        class HouseholdJob(Base):
            __tablename__ = "household_jobs"
            id: Mapped[int] = mapped_column(primary_key=True)

        class Report(NoMetaBase):
            __tablename__ = "reports"
            id: Mapped[int] = mapped_column(primary_key=True)

        stray = Table("stray", Base.metadata)
        """,
        monkeypatch,
    )

    findings = unmapped_table_findings([mapped], Path(), location="example")

    assert [(finding.check, finding.source) for finding in findings] == [
        (UNMAPPED_TABLE, "stray")
    ]


def test_a_table_stored_in_another_module_is_reported_where_it_is_stored():
    results = """
    from dataclasses import dataclass

    from sqlalchemy import Table

    from example.tables import household_jobs

    @dataclass
    class ResultRows:
        target: Table

    def job_rows(rows: ResultRows):
        return rows.target

    JOB_ROWS = ResultRows(target=household_jobs)
    """
    store = """
    from sqlalchemy import insert

    from example.results import JOB_ROWS, ResultRows

    def write(result_rows: ResultRows):
        return insert(result_rows.target)
    """
    sources = {"libs/example/src/example/results.py": results, STORE: store}

    assert _checks(sources, "libs/example/src/example/results.py") == [
        (CORE_TABLE_ACCESS, "rows.target"),
        (CORE_TABLE_ACCESS, "household_jobs"),
    ]
    assert _checks(sources, STORE) == []


@pytest.mark.parametrize(
    "expression", ["key.table", "job_id.table", "self.model.id.table", "rows.table"]
)
def test_any_table_read_in_a_module_using_sqlalchemy_is_core_table_access(
    expression,
):
    store = f"""
    from sqlalchemy import insert

    def write(self, key, job_id, rows):
        return insert({expression})
    """

    assert _checks({STORE: store}, STORE) == [(CORE_TABLE_ACCESS, expression)]


def test_nested_tuple_assignments_pair_each_name_with_its_value():
    store = """
    from example.tables import household_jobs, regions

    (current, label), area = (household_jobs.alias(), "jobs"), regions
    condition = current.c.id == area.c.id
    """

    assert sorted(source for _, source in _checks({STORE: store}, STORE)) == [
        "area",
        "current",
        "household_jobs",
        "regions",
    ]


def test_modules_importing_each_other_settle_on_their_exports():
    # first is read before second, so it learns that regions is a table only
    # on a later pass, once second's exports are known.
    first = """
    from example.second import regions
    from example.tables import household_jobs

    condition = regions.c.id == region_id
    """
    second = """
    from example.first import household_jobs
    from example.tables import regions

    condition = household_jobs.c.id == regions.c.id
    """
    sources = {
        "libs/example/src/example/first.py": first,
        "libs/example/src/example/second.py": second,
    }

    assert _checks(sources, "libs/example/src/example/first.py") == [
        (CORE_TABLE_ACCESS, "regions")
    ]
    assert _checks(sources, "libs/example/src/example/second.py") == [
        (CORE_TABLE_ACCESS, "household_jobs"),
        (CORE_TABLE_ACCESS, "regions"),
    ]


def test_a_called_or_assigned_table_attribute_is_not_a_table_read():
    store = """
    import sqlalchemy as sa

    class Report:
        def __init__(self, rows, model):
            self.table = model
            self.rows = rows.table(columns=["id"])
    """

    assert _checks({STORE: store}, STORE) == []


def test_a_class_attribute_assigned_a_core_table_is_one_where_read():
    store = """
    from example.tables import household_jobs

    class Index:
        table = household_jobs

    def identifiers(index: Index):
        return index.table.c.id
    """

    assert _checks({STORE: store}, STORE) == [
        (CORE_TABLE_ACCESS, "household_jobs"),
        (CORE_TABLE_ACCESS, "index.table"),
    ]


def test_a_tuple_assignment_pairs_each_name_with_its_value():
    store = """
    from example.tables import household_jobs

    current, previous = household_jobs.alias(), household_jobs.alias()
    label, count = "jobs", 2
    condition = previous.c.id == current.c.id
    """

    assert _checks({STORE: store}, STORE) == [
        (CORE_TABLE_ACCESS, "household_jobs"),
        (CORE_TABLE_ACCESS, "household_jobs"),
        (CORE_TABLE_ACCESS, "previous"),
        (CORE_TABLE_ACCESS, "current"),
    ]


def test_an_attribute_annotated_as_a_table_leaves_the_plain_name_alone():
    store = """
    from sqlalchemy import Table

    class Store:
        def __init__(self, source):
            self.jobs: Table = source

    def count(jobs: list[str]) -> int:
        return len(jobs)
    """

    assert [source for _, source in _checks({STORE: store}, STORE)] == []


def test_name_rules_apply_only_in_modules_that_use_sqlalchemy():
    gateway = """
    def publish(settings, socket, cache_engine, driver):
        settings.bigquery.table
        socket.connect()
        cache_engine.begin()
        driver.cursor()
    """

    assert _checks({GATEWAY: gateway}, GATEWAY) == []


@pytest.mark.parametrize(
    "table_expression",
    [
        "HouseholdJob.__table__",
        "inspect(HouseholdJob).local_table",
        "inspect(HouseholdJob).persist_selectable",
        "Base.metadata.sorted_tables",
    ],
)
def test_a_name_assigned_a_table_attribute_stays_a_core_table(table_expression):
    models = f"""
    from example.orm_base import Base, HouseholdJob
    from sqlalchemy import inspect, select

    jobs = {table_expression}
    statement = select(jobs)
    """

    assert _checks({MODELS: models}, MODELS) == [
        (CORE_TABLE_ACCESS, table_expression),
        (CORE_TABLE_ACCESS, "jobs"),
    ]


def test_a_mapped_classs_table_stays_a_core_table_through_imports():
    """A store keeps its findings when its table becomes ``X.__table__``."""
    models = """
    from example.orm_base import HouseholdJob

    household_jobs = HouseholdJob.__table__
    """
    package = """
    from example.models import household_jobs
    """
    store = """
    from example import household_jobs as jobs
    from example import models
    from sqlalchemy import select

    by_name = select(jobs)
    by_module = select(models.household_jobs)
    """

    assert _checks({MODELS: models, PACKAGE: package, STORE: store}, STORE) == [
        (CORE_TABLE_ACCESS, "jobs"),
        (CORE_TABLE_ACCESS, "models.household_jobs"),
    ]


def test_an_attribute_assigned_a_mapped_classs_table_is_one_where_read():
    store = """
    from example.orm_base import HouseholdJob
    from sqlalchemy import select

    class Store:
        def __init__(self):
            self.jobs = HouseholdJob.__table__

        def statement(self):
            return select(self.jobs)
    """

    assert _checks({STORE: store}, STORE) == [
        (CORE_TABLE_ACCESS, "HouseholdJob.__table__"),
        (CORE_TABLE_ACCESS, "self.jobs"),
    ]


def test_a_name_assigned_a_mapped_class_or_its_table_name_is_not_a_table():
    store = """
    from example.orm_base import HouseholdJob
    from sqlalchemy import select

    job_model = HouseholdJob
    table_name = HouseholdJob.__tablename__
    statement = select(job_model).where(job_model.id == table_name)
    """

    assert _checks({STORE: store}, STORE) == []
