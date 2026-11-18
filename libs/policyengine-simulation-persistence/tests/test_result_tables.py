"""Sim's models of the Public API's result tables, which the Modal worker writes."""

from __future__ import annotations

from policyengine_simulation_persistence import (
    ReportRun,
    SimulationRun,
    result_metadata,
    service_metadata,
)
from sqlalchemy import Enum

# Results that compare a report run's two simulation runs belong to the report
# run; poverty and inequality, which describe one simulation, to a simulation
# run.
REPORT_RUN_RESULT_TABLES = {
    "budget_summary",
    "congressional_district_impacts",
    "constituency_impacts",
    "decile_impacts",
    "intra_decile_impacts",
    "labor_supply_decile_responses",
    "labor_supply_responses",
    "local_authority_impacts",
    "program_statistics",
    "withheld_area_codes",
}
SIMULATION_RUN_RESULT_TABLES = {"inequality", "poverty"}


def test_the_result_models_are_the_twelve_result_tables() -> None:
    modelled = {table.name for table in result_metadata.tables.values()}

    assert modelled == REPORT_RUN_RESULT_TABLES | SIMULATION_RUN_RESULT_TABLES


def test_the_result_models_share_no_table_with_the_service_models() -> None:
    assert not set(result_metadata.tables) & set(service_metadata.tables)


def test_each_result_names_the_service_models_run_it_belongs_to() -> None:
    for table in result_metadata.tables.values():
        referred_tables = {
            constraint.column_keys[0]: constraint.referred_table
            for constraint in table.foreign_key_constraints
        }
        if table.name in SIMULATION_RUN_RESULT_TABLES:
            assert referred_tables == {"simulation_run_id": SimulationRun.__table__}, (
                table
            )
            continue
        assert referred_tables["report_run_id"] is ReportRun.__table__, table
        if table.name.startswith("labor_supply_"):
            assert set(referred_tables) == {"report_run_id"}, table
        else:
            assert (
                referred_tables["baseline_simulation_run_id"] is SimulationRun.__table__
            )
            assert (
                referred_tables["reform_simulation_run_id"] is SimulationRun.__table__
            )


def test_every_result_table_and_enum_type_names_the_public_schema() -> None:
    for table in result_metadata.tables.values():
        assert table.schema == "public", table
        for column in table.columns:
            if isinstance(column.type, Enum):
                assert column.type.schema == "public", column
