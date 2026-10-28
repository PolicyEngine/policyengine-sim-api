"""Describe Public API tables, to check sim's test copies against the real ones.

Sim's tests create copies of the Public API tables sim reads and refers to
(``public_api_support``), with only the columns sim uses. A copy that differs
from the real table hides bugs: a ``varchar`` copy of the enum column
``regions.region_type`` let a comparison PostgreSQL refuses pass every test.

``describe_tables`` reads, from a database's catalog, each table's columns
(their type, with an enum named as ``enum <type>``, and whether they allow
null), its primary key and unique constraints (by name, with their columns),
and the values of each enum its columns use. ``schema_differences`` lists
every way the copies differ from the real tables: for each table the copies
create, each column they define must have the real column's type and
nullability, each primary key and unique constraint they define must exist
under the same name with the same columns, and each enum must have the same
values in the same order. Columns, constraints and tables the copies leave
out are not compared, so a table or column added to the copies is checked
with no change here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

    from sqlalchemy import Connection

ENUM_PREFIX = "enum "

_COLUMNS = text(
    "SELECT c.relname AS table_name, a.attname AS column_name, "
    "CASE WHEN t.typtype = 'e' THEN 'enum ' || t.typname "
    "ELSE format_type(a.atttypid, a.atttypmod) END AS column_type, "
    "NOT a.attnotnull AS nullable "
    "FROM pg_attribute a "
    "JOIN pg_class c ON c.oid = a.attrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_type t ON t.oid = a.atttypid "
    "WHERE n.nspname = 'public' AND c.relkind = 'r' "
    "AND a.attnum > 0 AND NOT a.attisdropped "
    "AND c.relname = ANY(:table_names) "
    "ORDER BY c.relname, a.attnum"
)
_CONSTRAINTS = text(
    "SELECT c.relname AS table_name, con.conname AS constraint_name, "
    "array_agg(a.attname::text ORDER BY key.position) AS column_names "
    "FROM pg_constraint con "
    "JOIN pg_class c ON c.oid = con.conrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY "
    "AS key(attribute_number, position) "
    "JOIN pg_attribute a ON a.attrelid = con.conrelid "
    "AND a.attnum = key.attribute_number "
    "WHERE n.nspname = 'public' AND con.contype IN ('p', 'u') "
    "AND c.relname = ANY(:table_names) "
    "GROUP BY c.relname, con.conname"
)
_ENUM_VALUES = text(
    "SELECT t.typname AS enum_name, "
    "array_agg(e.enumlabel::text ORDER BY e.enumsortorder) AS enum_values "
    "FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
    "JOIN pg_namespace n ON n.oid = t.typnamespace "
    "WHERE n.nspname = 'public' GROUP BY t.typname"
)
_PUBLIC_TABLES = text(
    "SELECT c.relname FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY c.relname"
)


@dataclass(frozen=True)
class ColumnShape:
    """A column's type, as PostgreSQL formats it, and whether it allows null."""

    column_type: str
    nullable: bool


@dataclass(frozen=True)
class TableShape:
    """A table's columns, and its primary key and unique constraints by name."""

    columns: dict[str, ColumnShape]
    unique_constraints: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class SchemaShape:
    """Tables by name, and the values of each enum their columns use."""

    tables: dict[str, TableShape]
    enums: dict[str, tuple[str, ...]]


def public_tables(connection: Connection) -> list[str]:
    """Return the names of the tables in the ``public`` schema."""
    return list(connection.execute(_PUBLIC_TABLES).scalars())


def describe_tables(
    connection: Connection, table_names: Collection[str]
) -> SchemaShape:
    """Describe these tables of the ``public`` schema from the database's catalog."""
    names = sorted(table_names)
    columns: dict[str, dict[str, ColumnShape]] = {}
    for row in connection.execute(_COLUMNS, {"table_names": names}).mappings():
        columns.setdefault(row["table_name"], {})[row["column_name"]] = ColumnShape(
            column_type=row["column_type"], nullable=row["nullable"]
        )
    constraints: dict[str, dict[str, tuple[str, ...]]] = {}
    for row in connection.execute(_CONSTRAINTS, {"table_names": names}).mappings():
        constraints.setdefault(row["table_name"], {})[row["constraint_name"]] = tuple(
            row["column_names"]
        )
    used_enums = {
        column.column_type.removeprefix(ENUM_PREFIX)
        for table_columns in columns.values()
        for column in table_columns.values()
        if column.column_type.startswith(ENUM_PREFIX)
    }
    enums = {
        row["enum_name"]: tuple(row["enum_values"])
        for row in connection.execute(_ENUM_VALUES).mappings()
        if row["enum_name"] in used_enums
    }
    return SchemaShape(
        tables={
            name: TableShape(
                columns=columns[name], unique_constraints=constraints.get(name, {})
            )
            for name in names
            if name in columns
        },
        enums=enums,
    )


def _table_differences(name: str, copy: TableShape, real: TableShape) -> list[str]:
    found = []
    for column_name, copied in copy.columns.items():
        actual = real.columns.get(column_name)
        if actual is None:
            found.append(
                f"{name}.{column_name}: the Public API's table has no such column"
            )
            continue
        if copied.column_type != actual.column_type:
            found.append(
                f"{name}.{column_name}: the copy is {copied.column_type}, "
                f"the Public API's is {actual.column_type}"
            )
        if copied.nullable != actual.nullable:
            found.append(
                f"{name}.{column_name}: the copy "
                f"{'allows' if copied.nullable else 'refuses'} null, the Public "
                f"API's {'allows' if actual.nullable else 'refuses'} it"
            )
    for constraint_name, copied_columns in copy.unique_constraints.items():
        actual_columns = real.unique_constraints.get(constraint_name)
        if actual_columns is None:
            found.append(
                f"{name}: the Public API's table has no constraint "
                f"{constraint_name} ({', '.join(copied_columns)})"
            )
        elif actual_columns != copied_columns:
            found.append(
                f"{name}: {constraint_name} is ({', '.join(copied_columns)}) in the "
                f"copy, ({', '.join(actual_columns)}) in the Public API"
            )
    return found


def schema_differences(copies: SchemaShape, real: SchemaShape) -> list[str]:
    """Return every way the copies differ from the real tables, or nothing."""
    found = []
    for name, copy in copies.tables.items():
        actual = real.tables.get(name)
        if actual is None:
            found.append(f"{name}: the Public API has no such table")
            continue
        found.extend(_table_differences(name, copy, actual))
    for enum_name, copied_values in copies.enums.items():
        actual_values = real.enums.get(enum_name)
        if actual_values is not None and actual_values != copied_values:
            found.append(
                f"enum {enum_name}: the copy has ({', '.join(copied_values)}), the "
                f"Public API has ({', '.join(actual_values)})"
            )
    return found


def schema_to_json(schema: SchemaShape) -> str:
    """Return the description as indented JSON, for a fixture file."""
    payload = {
        "tables": {
            name: {
                "columns": {
                    column_name: {
                        "type": column.column_type,
                        "nullable": column.nullable,
                    }
                    for column_name, column in table.columns.items()
                },
                "unique_constraints": {
                    constraint_name: list(columns)
                    for constraint_name, columns in sorted(
                        table.unique_constraints.items()
                    )
                },
            }
            for name, table in schema.tables.items()
        },
        "enums": {name: list(values) for name, values in schema.enums.items()},
    }
    return json.dumps(payload, indent=2) + "\n"


def schema_from_json(path: Path) -> SchemaShape:
    """Read a description that ``schema_to_json`` wrote."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return SchemaShape(
        tables={
            name: TableShape(
                columns={
                    column_name: ColumnShape(
                        column_type=column["type"], nullable=column["nullable"]
                    )
                    for column_name, column in table["columns"].items()
                },
                unique_constraints={
                    constraint_name: tuple(columns)
                    for constraint_name, columns in table["unique_constraints"].items()
                },
            )
            for name, table in payload["tables"].items()
        },
        enums={name: tuple(values) for name, values in payload["enums"].items()},
    )
