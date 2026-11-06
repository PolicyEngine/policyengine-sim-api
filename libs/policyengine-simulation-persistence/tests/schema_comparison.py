"""Describe tables from PostgreSQL's catalog, and list how two descriptions differ.

Sim's table models describe tables the Public API's migration chain creates.
The comparison builds the models' tables in one database and reads both that
database and one the chain built through the same catalog queries, so every
type, default and definition is written the way PostgreSQL itself writes it.

``describe_tables`` reads, for each table: its columns (type, with an enum
named as ``enum <type>``; whether they allow null; their default), its
constraints (primary key, unique, foreign key, check and exclusion) by name
with PostgreSQL's definition, and its other indexes by name with their
definition; and the values of each enum its columns use.
``schema_differences`` lists every way the models' tables differ from the
real ones. Tables the models leave out are not compared.
``foreign_key_targets`` reads which table each foreign key names.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

    from sqlalchemy import Connection

ENUM_PREFIX = "enum "
# An identifier as PostgreSQL writes it: quoted, with "" for a quote, or bare.
_IDENTIFIER = r'"(?:[^"]|"")+"|[^\s".(]+'
# The table a foreign key's definition names. PostgreSQL qualifies it by its
# schema whenever the bare name wouldn't find that table on the search path.
_FOREIGN_KEY_TARGET = re.compile(
    rf"^FOREIGN KEY \(.+?\) REFERENCES (?:(?P<schema>{_IDENTIFIER})\.)?"
    rf"(?P<table>{_IDENTIFIER})\("
)

_COLUMNS = text(
    "SELECT c.relname AS table_name, a.attname AS column_name, "
    "CASE WHEN t.typtype = 'e' THEN 'enum ' || t.typname "
    "ELSE format_type(a.atttypid, a.atttypmod) END AS column_type, "
    "NOT a.attnotnull AS nullable, "
    "pg_get_expr(d.adbin, d.adrelid) AS column_default "
    "FROM pg_attribute a "
    "JOIN pg_class c ON c.oid = a.attrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_type t ON t.oid = a.atttypid "
    "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
    "AND a.attnum > 0 AND NOT a.attisdropped "
    "AND c.relname = ANY(:table_names) "
    "ORDER BY c.relname, a.attnum"
)
_CONSTRAINTS = text(
    "SELECT c.relname AS table_name, con.conname AS constraint_name, "
    "pg_get_constraintdef(con.oid) AS definition "
    "FROM pg_constraint con "
    "JOIN pg_class c ON c.oid = con.conrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND con.contype IN ('p', 'u', 'f', 'c', 'x') "
    "AND c.relname = ANY(:table_names) "
    "ORDER BY c.relname, con.conname"
)
# Indexes that back a primary key, unique or exclusion constraint are compared
# as that constraint.
_INDEXES = text(
    "SELECT t.relname AS table_name, i.relname AS index_name, "
    "pg_get_indexdef(i.oid) AS definition "
    "FROM pg_index x "
    "JOIN pg_class i ON i.oid = x.indexrelid "
    "JOIN pg_class t ON t.oid = x.indrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE n.nspname = 'public' AND t.relname = ANY(:table_names) "
    "AND NOT EXISTS (SELECT 1 FROM pg_constraint con "
    "WHERE con.conindid = x.indexrelid AND con.contype IN ('p', 'u', 'x')) "
    "ORDER BY t.relname, i.relname"
)
_ENUM_VALUES = text(
    "SELECT t.typname AS enum_name, "
    "array_agg(e.enumlabel::text ORDER BY e.enumsortorder) AS enum_values "
    "FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
    "JOIN pg_namespace n ON n.oid = t.typnamespace "
    "WHERE n.nspname = 'public' GROUP BY t.typname"
)


@dataclass(frozen=True)
class ColumnShape:
    """A column's type, nullability and default, as PostgreSQL writes them."""

    column_type: str
    nullable: bool
    default: str | None


@dataclass(frozen=True)
class TableShape:
    """A table's columns, its constraints and its other indexes, by name."""

    columns: dict[str, ColumnShape]
    constraints: dict[str, str]
    indexes: dict[str, str]


@dataclass(frozen=True)
class SchemaShape:
    """Tables by name, and the values of each enum their columns use."""

    tables: dict[str, TableShape]
    enums: dict[str, tuple[str, ...]]


def describe_tables(
    connection: Connection, table_names: Collection[str]
) -> SchemaShape:
    """Describe these tables of the ``public`` schema from the database's catalog."""
    names = sorted(table_names)
    parameters = {"table_names": names}
    columns: dict[str, dict[str, ColumnShape]] = {}
    for row in connection.execute(_COLUMNS, parameters).mappings():
        columns.setdefault(row["table_name"], {})[row["column_name"]] = ColumnShape(
            column_type=row["column_type"],
            nullable=row["nullable"],
            default=row["column_default"],
        )
    constraints: dict[str, dict[str, str]] = {}
    for row in connection.execute(_CONSTRAINTS, parameters).mappings():
        constraints.setdefault(row["table_name"], {})[row["constraint_name"]] = row[
            "definition"
        ]
    indexes: dict[str, dict[str, str]] = {}
    for row in connection.execute(_INDEXES, parameters).mappings():
        indexes.setdefault(row["table_name"], {})[row["index_name"]] = row["definition"]
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
                columns=columns[name],
                constraints=constraints.get(name, {}),
                indexes=indexes.get(name, {}),
            )
            for name in names
            if name in columns
        },
        enums=enums,
    )


def _default_difference(model: str | None, real: str | None) -> str:
    model_default = "has no default" if model is None else f"defaults to {model}"
    real_default = "has no default" if real is None else f"defaults to {real}"
    return f"the model {model_default}, the Public API's {real_default}"


def _column_differences(
    table_name: str, models: TableShape, real: TableShape
) -> list[str]:
    found = []
    for column_name, modelled in models.columns.items():
        actual = real.columns.get(column_name)
        where = f"{table_name}.{column_name}"
        if actual is None:
            found.append(f"{where}: the Public API's table has no such column")
            continue
        if modelled.column_type != actual.column_type:
            found.append(
                f"{where}: the model is {modelled.column_type}, the Public API's "
                f"is {actual.column_type}"
            )
        if modelled.nullable != actual.nullable:
            found.append(
                f"{where}: the model "
                f"{'allows' if modelled.nullable else 'refuses'} null, the Public "
                f"API's {'allows' if actual.nullable else 'refuses'} it"
            )
        if modelled.default != actual.default:
            found.append(
                f"{where}: {_default_difference(modelled.default, actual.default)}"
            )
    found.extend(
        f"{table_name}.{column_name}: the model has no such column"
        for column_name in real.columns
        if column_name not in models.columns
    )
    return found


def _definition_differences(
    table_name: str,
    kind: str,
    models: dict[str, str],
    real: dict[str, str],
) -> list[str]:
    found = []
    for name, modelled in models.items():
        actual = real.get(name)
        if actual is None:
            found.append(
                f"{table_name}: the Public API's table has no {kind} {name} "
                f"({modelled})"
            )
        elif actual != modelled:
            found.append(
                f"{table_name}: {name} is {modelled} in the model, {actual} in "
                "the Public API"
            )
    found.extend(
        f"{table_name}: the model has no {kind} {name} ({actual})"
        for name, actual in real.items()
        if name not in models
    )
    return found


def schema_differences(models: SchemaShape, real: SchemaShape) -> list[str]:
    """Return every way the models' tables differ from the real ones, or nothing."""
    found = []
    for table_name, modelled in models.tables.items():
        actual = real.tables.get(table_name)
        if actual is None:
            found.append(f"{table_name}: the Public API has no such table")
            continue
        found.extend(_column_differences(table_name, modelled, actual))
        found.extend(
            _definition_differences(
                table_name, "constraint", modelled.constraints, actual.constraints
            )
        )
        found.extend(
            _definition_differences(
                table_name, "index", modelled.indexes, actual.indexes
            )
        )
    for enum_name, modelled_values in models.enums.items():
        actual_values = real.enums.get(enum_name)
        if actual_values is None:
            # Enum columns are named without their schema, so an enum moved out
            # of the public schema would otherwise pass unchecked.
            found.append(
                f"enum {enum_name}: the Public API has no such enum in its public "
                "schema"
            )
        elif actual_values != modelled_values:
            found.append(
                f"enum {enum_name}: the model has ({', '.join(modelled_values)}), "
                f"the Public API has ({', '.join(actual_values)})"
            )
    return found


def schema_to_json(schema: SchemaShape) -> str:
    """Write a description as JSON, as the rule's proof fixtures store it."""
    return json.dumps(asdict(schema), indent=2, sort_keys=True) + "\n"


def schema_from_json(path: Path) -> SchemaShape:
    """Read a description that ``schema_to_json`` wrote."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return SchemaShape(
        tables={
            name: TableShape(
                columns={
                    column_name: ColumnShape(**column)
                    for column_name, column in table["columns"].items()
                },
                constraints=dict(table["constraints"]),
                indexes=dict(table["indexes"]),
            )
            for name, table in payload["tables"].items()
        },
        enums={name: tuple(values) for name, values in payload["enums"].items()},
    )


def _unquoted(identifier: str) -> str:
    if identifier.startswith('"'):
        return identifier[1:-1].replace('""', '"')
    return identifier


def foreign_key_targets(schema: SchemaShape) -> dict[tuple[str, str], str]:
    """Return the table each foreign key names, by its table and constraint name.

    The table is unquoted, and keeps its schema when PostgreSQL wrote one, as
    ``auth.users``. Raise ValueError for a foreign key whose definition names
    no table this can read, so no key goes uncounted.
    """
    targets: dict[tuple[str, str], str] = {}
    for table_name, table in schema.tables.items():
        for constraint_name, definition in table.constraints.items():
            if not definition.startswith("FOREIGN KEY "):
                continue
            target = _FOREIGN_KEY_TARGET.match(definition)
            if target is None:
                message = (
                    f"{table_name}.{constraint_name}: no table read from {definition!r}"
                )
                raise ValueError(message)
            named_table = _unquoted(target["table"])
            if target["schema"] is not None:
                named_table = f"{_unquoted(target['schema'])}.{named_table}"
            targets[(table_name, constraint_name)] = named_table
    return targets
