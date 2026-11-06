"""Sim's models of the Public API's tables the simulation service writes."""

from __future__ import annotations

from policyengine_simulation_persistence import (
    SERVICE_TABLE_PRIVILEGES,
    SimulationStatus,
    report_runs,
    reports,
    service_metadata,
    simulation_runs,
)
from policyengine_simulation_persistence import tables as tables_module
from sqlalchemy import Enum, Table, UniqueConstraint


def _foreign_keys(table: Table) -> dict[str, tuple[tuple[str, ...], str, str]]:
    """Return each foreign key by name: its columns, the key it names, its rule."""
    return {
        str(constraint.name): (
            tuple(constraint.column_keys),
            f"{constraint.referred_table.name}"
            f"({', '.join(element.column.name for element in constraint.elements)})",
            str(constraint.ondelete),
        )
        for constraint in table.foreign_key_constraints
    }


def _unique_keys(table: Table) -> dict[str, tuple[str, ...]]:
    return {
        str(constraint.name): tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _indexed_columns(table: Table, index_name: str) -> tuple[str, ...]:
    (index,) = (index for index in table.indexes if index.name == index_name)
    return tuple(column.name for column in index.columns)


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


def test_every_enum_type_names_the_public_schema() -> None:
    """Enum types resolve to the Public API's whatever the SQLAlchemy release.

    SQLAlchemy 2.1 gives an enum attached to a table the metadata's schema,
    2.0 doesn't, so the types the tables use are checked, and so is the type
    the helper builds before any table adopts it.
    """
    enum_columns = [
        column
        for table in service_metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Enum)
    ]

    assert len(enum_columns) == 7
    for column in enum_columns:
        assert column.type.schema == "public", f"{column.table.name}.{column.name}"
    # The helper every enum column uses, before any table adopts its type.
    helper_type = tables_module._enum_type(SimulationStatus, "v2_example")
    assert helper_type.schema == "public"


def test_a_report_run_names_its_baseline_and_reform_simulation_runs() -> None:
    """A report run compares one pair; no table lists a report run's members."""
    assert report_runs.c.baseline_simulation_run_id.nullable is False
    # A household report with only a baseline has no reform run.
    assert report_runs.c.reform_simulation_run_id.nullable is True
    keys = _foreign_keys(report_runs)

    assert keys["fk_report_runs_baseline_simulation_run_id_simulation_runs"] == (
        ("baseline_simulation_run_id",),
        "simulation_runs(id)",
        "RESTRICT",
    )
    assert keys["fk_report_runs_reform_simulation_run_id_simulation_runs"] == (
        ("reform_simulation_run_id",),
        "simulation_runs(id)",
        "RESTRICT",
    )
    assert "report_run_simulation_runs" not in {
        table.name for table in service_metadata.tables.values()
    }


def test_a_budget_window_has_one_report_run_per_year_of_one_report() -> None:
    """A window compares the report's two simulations in each of its years."""
    assert reports.c.budget_window_size.nullable is True
    assert simulation_runs.c.year.nullable is False
    assert _indexed_columns(simulation_runs, "ix_simulation_runs_current_output")[
        :2
    ] == ("simulation_id", "year")

    # The window run must belong to the year run's own report, and owns it.
    assert _foreign_keys(report_runs)[
        "fk_report_runs_budget_window_run_id_budget_window_runs"
    ] == (
        ("budget_window_run_id", "report_id"),
        "budget_window_runs(id, report_id)",
        "CASCADE",
    )
    assert _unique_keys(report_runs)["uq_report_runs_budget_window_run_id_year"] == (
        "budget_window_run_id",
        "year",
    )
