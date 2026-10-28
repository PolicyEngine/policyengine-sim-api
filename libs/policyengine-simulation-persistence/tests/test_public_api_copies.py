"""The tests' copies of the Public API tables match the Public API's columns.

The household reads (``household_reads``) select columns of the Public API's
tables. The tests create copies of those tables (``public_api_support``); a
copy whose column has another type or nullability than the real one would
let a read pass here and fail in production, as a text comparison with a
PostgreSQL enum column once did. ``fixtures/public_api/household_read_columns.json``
records each column read as the Public API's migrations create it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from policyengine_simulation_persistence.household_reads import (
    household_read_metadata,
)
from public_api_support import superuser_engine
from sqlalchemy import text

RECORDED_COLUMNS = (
    Path(__file__).parent / "fixtures" / "public_api" / "household_read_columns.json"
)


def _recorded() -> dict[str, dict[str, dict[str, object]]]:
    return json.loads(RECORDED_COLUMNS.read_text(encoding="utf-8"))["tables"]


def _copied(superuser_url: str, tables: list[str]) -> dict[str, dict[str, dict]]:
    engine = superuser_engine(superuser_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT table_name, column_name, udt_name, is_nullable, "
                    "character_maximum_length FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = ANY(:tables)"
                ),
                {"tables": tables},
            ).all()
    finally:
        engine.dispose()
    copied: dict[str, dict[str, dict]] = {}
    for table, column, column_type, nullable, length in rows:
        description: dict[str, object] = {
            "type": column_type,
            "nullable": nullable == "YES",
        }
        if length is not None:
            description["length"] = length
        copied.setdefault(table, {})[column] = description
    return copied


@pytest.mark.postgres
def test_each_column_the_household_reads_select_matches_the_public_api(
    disposable_database,
) -> None:
    recorded = _recorded()

    copied = _copied(disposable_database.superuser_url, sorted(recorded))

    mismatches = [
        f"{table}.{column}: copy {copied.get(table, {}).get(column)!r}, "
        f"Public API {description!r}"
        for table, columns in recorded.items()
        for column, description in columns.items()
        if copied.get(table, {}).get(column) != description
    ]
    assert mismatches == []


def test_every_column_the_household_reads_select_is_recorded() -> None:
    """A column added to a read must be recorded, so its copy is checked too."""
    recorded = _recorded()

    unrecorded = [
        f"{table.name}.{column.name}"
        for table in household_read_metadata.tables.values()
        for column in table.columns
        if column.name not in recorded.get(table.name, {})
    ]

    assert unrecorded == []
