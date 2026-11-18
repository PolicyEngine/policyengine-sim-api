"""The ORM mapped classes of the comparison path's two tables."""

from __future__ import annotations

import inspect as python_inspect
from types import NoneType, UnionType
from typing import Union, get_args, get_origin

import pytest
from policyengine_stage12_persistence.orm_base import ComparisonBase
from policyengine_stage12_persistence.tables import (
    REPORT_TABLE_NAME,
    SIMULATION_TABLE_NAME,
    ComparisonReport,
    ComparisonSimulation,
    comparison_reports,
    comparison_simulations,
    metadata,
)
from sqlalchemy import inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, configure_mappers

MAPPED_CLASSES = [
    pytest.param(
        ComparisonReport,
        comparison_reports,
        REPORT_TABLE_NAME,
        "evaluation_id",
        id="report",
    ),
    pytest.param(
        ComparisonSimulation,
        comparison_simulations,
        SIMULATION_TABLE_NAME,
        "simulation_execution_id",
        id="simulation",
    ),
]


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
