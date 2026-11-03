"""Sim's models of the Public API's tables the simulation service writes."""

from __future__ import annotations

from policyengine_simulation_persistence import (
    SERVICE_TABLE_PRIVILEGES,
    service_metadata,
)


def test_the_models_describe_every_table_the_service_creates_rows_in() -> None:
    created_tables = {
        table_name
        for table_name, privileges in SERVICE_TABLE_PRIVILEGES.items()
        if "INSERT" in privileges
    }

    assert {table.name for table in service_metadata.tables.values()} == (
        created_tables
    )


def test_foreign_keys_stay_between_the_services_own_tables() -> None:
    """Another service writes the policy, household and catalog rows."""
    for table in service_metadata.tables.values():
        for foreign_key in table.foreign_keys:
            assert foreign_key.column.table in service_metadata.tables.values(), (
                f"{table.name}.{foreign_key.parent.name}"
            )


def test_every_table_names_the_public_schema() -> None:
    """Writes go to the Public API's tables whatever the role's search_path."""
    for table in service_metadata.tables.values():
        assert table.schema == "public", table.name
