"""Sim's models of the Public API's tables the simulation service writes."""

from __future__ import annotations

from policyengine_simulation_persistence import (
    SERVICE_TABLE_PRIVILEGES,
    SimulationStatus,
    household_jobs,
    referenced_metadata,
    report_runs,
    reports,
    service_metadata,
    simulation_runs,
)
from policyengine_simulation_persistence import tables as tables_module
from public_api_keys import KEYS_TO_PUBLIC_API_TABLES
from sqlalchemy import Enum, ForeignKeyConstraint, Table, UniqueConstraint


def _key(constraint: ForeignKeyConstraint) -> tuple[tuple[str, ...], str, str]:
    """Return a foreign key's columns, the columns it names, and its delete rule."""
    named_columns = ", ".join(element.column.name for element in constraint.elements)
    return (
        tuple(constraint.column_keys),
        f"{constraint.referred_table.name}({named_columns})",
        str(constraint.ondelete),
    )


def _foreign_keys(table: Table) -> dict[str, tuple[tuple[str, ...], str, str]]:
    return {
        str(constraint.name): _key(constraint)
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


def test_the_models_declare_every_key_to_the_public_apis_own_tables() -> None:
    """The Public API writes the rows these keys name; PostgreSQL checks them."""
    service_tables = set(service_metadata.tables.values())
    keys_to_public_api_tables = {
        (table.name, str(constraint.name)): _key(constraint)
        for table in service_tables
        for constraint in table.foreign_key_constraints
        if constraint.referred_table not in service_tables
    }

    assert keys_to_public_api_tables == KEYS_TO_PUBLIC_API_TABLES
    assert len(KEYS_TO_PUBLIC_API_TABLES) == 20


def test_the_public_apis_tables_are_modelled_by_their_id_alone() -> None:
    """Enough for SQLAlchemy to declare each key, and nothing to read or write."""
    named_tables = {
        constraint.referred_table
        for table in service_metadata.tables.values()
        for constraint in table.foreign_key_constraints
        if constraint.referred_table.metadata is referenced_metadata
    }

    assert {table.name for table in named_tables} == {
        "datasets",
        "dynamics",
        "households",
        "policies",
        "regions",
        "tax_benefit_model_versions",
        "tax_benefit_models",
        "users",
    }
    assert set(referenced_metadata.tables.values()) == named_tables
    for table in named_tables:
        assert [column.name for column in table.columns] == ["id"], table.name
        assert table.schema == "public", table.name


def test_the_columns_the_public_apis_deletes_search_are_indexed() -> None:
    """Deleting a user or model version searches these columns for its rows."""
    assert _indexed_columns(reports, "ix_reports_user_id") == ("user_id",)
    assert _indexed_columns(
        simulation_runs, "ix_simulation_runs_tax_benefit_model_version_id"
    ) == ("tax_benefit_model_version_id",)
    assert _indexed_columns(
        household_jobs, "ix_household_jobs_tax_benefit_model_version_id"
    ) == ("tax_benefit_model_version_id",)


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
