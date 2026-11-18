"""The ORM mapped classes of the comparison path's two tables."""

from __future__ import annotations

import inspect as python_inspect
import os
from datetime import UTC, datetime, timedelta
from types import NoneType, UnionType
from typing import Union, get_args, get_origin
from uuid import uuid4

import pytest
from policyengine_stage12_persistence.orm_base import ComparisonBase
from policyengine_stage12_persistence.tables import (
    REPORT_TABLE_NAME,
    SIMULATION_TABLE_NAME,
    ComparisonReport,
    ComparisonSimulation,
    metadata,
)
from report_run_database import SEED_URL_ENV, worker_database_url
from sqlalchemy import FetchedValue, create_engine, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, Session, configure_mappers
from test_store import _report, _simulation

MAPPED_CLASSES = [
    pytest.param(
        ComparisonReport,
        ComparisonReport.__table__,
        REPORT_TABLE_NAME,
        "evaluation_id",
        id="report",
    ),
    pytest.param(
        ComparisonSimulation,
        ComparisonSimulation.__table__,
        SIMULATION_TABLE_NAME,
        "simulation_execution_id",
        id="simulation",
    ),
]
# The columns API v1's tables give a server default.
REPORT_DATABASE_DEFAULTS = {"comparison_status", "created_at", "updated_at"}
SIMULATION_DATABASE_DEFAULTS = {"created_at", "updated_at"}


def _allows_none(annotation: object) -> bool:
    return get_origin(annotation) in (Union, UnionType) and NoneType in get_args(
        annotation
    )


def test_the_base_maps_exactly_the_comparison_tables() -> None:
    assert ComparisonBase.metadata is metadata
    assert {mapper.class_ for mapper in ComparisonBase.registry.mappers} == {
        ComparisonReport,
        ComparisonSimulation,
    }
    configure_mappers()


@pytest.mark.parametrize(
    ("mapped_class", "table", "table_name", "primary_key"), MAPPED_CLASSES
)
def test_each_table_name_the_store_imports_is_its_classs_table(
    mapped_class, table, table_name, primary_key
) -> None:
    assert mapped_class.__table__ is table
    assert mapped_class.__tablename__ == table_name
    assert [column.name for column in inspect(mapped_class).primary_key] == [
        primary_key
    ]


@pytest.mark.parametrize(
    ("mapped_class", "table", "table_name", "primary_key"), MAPPED_CLASSES
)
def test_each_column_is_a_typed_attribute_of_its_own_name(
    mapped_class, table, table_name, primary_key
) -> None:
    annotations = python_inspect.get_annotations(mapped_class, eval_str=True)

    assert [attribute.key for attribute in inspect(mapped_class).column_attrs] == [
        column.name for column in table.columns
    ]
    for column in table.columns:
        annotation = annotations[column.name]
        assert get_origin(annotation) is Mapped, column.name
        (value_type,) = get_args(annotation)
        assert _allows_none(value_type) is column.nullable, column.name


def test_a_statement_on_a_class_names_its_table_in_the_public_schema() -> None:
    statement = select(ComparisonSimulation).where(
        ComparisonSimulation.status == "pending"
    )

    assert f"FROM public.{SIMULATION_TABLE_NAME}" in str(
        statement.compile(dialect=postgresql.dialect())
    )


@pytest.mark.parametrize(
    ("table", "column_names"),
    [
        pytest.param(ComparisonReport.__table__, REPORT_DATABASE_DEFAULTS, id="report"),
        pytest.param(
            ComparisonSimulation.__table__,
            SIMULATION_DATABASE_DEFAULTS,
            id="simulation",
        ),
    ],
)
def test_the_columns_the_database_fills_are_marked_as_such(table, column_names) -> None:
    marked = {
        column.name
        for column in table.columns
        if isinstance(column.server_default, FetchedValue)
    }

    assert marked == column_names


def test_a_session_insert_leaves_the_tables_defaults_to_the_database() -> None:
    worker_database_url()
    report_id = uuid4()
    # The tables require retention to end after the database's created_at.
    retention_expires_at = datetime.now(UTC) + timedelta(days=30)
    report_values = {
        **_report().model_dump(exclude=REPORT_DATABASE_DEFAULTS),
        "evaluation_id": report_id,
        "retention_expires_at": retention_expires_at,
    }
    simulation_values = {
        **_simulation().model_dump(exclude=SIMULATION_DATABASE_DEFAULTS),
        "simulation_execution_id": uuid4(),
        "evaluation_id": report_id,
        "retention_expires_at": retention_expires_at,
    }
    report = ComparisonReport(**report_values)
    simulation = ComparisonSimulation(**simulation_values)
    engine = create_engine(os.environ[SEED_URL_ENV])
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            with Session(
                connection, join_transaction_mode="create_savepoint"
            ) as session:
                session.add(report)
                session.flush()
                session.add(simulation)
                session.flush()
                stored_status = report.comparison_status
                report_created_at = report.created_at
                simulation_created_at = simulation.created_at
            transaction.rollback()
    finally:
        engine.dispose()

    assert stored_status == "not_requested"
    assert report_created_at is not None
    assert simulation_created_at is not None
