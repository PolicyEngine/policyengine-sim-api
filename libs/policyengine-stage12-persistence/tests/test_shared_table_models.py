"""The Modal worker role's stores use the simulation service's table models.

``policyengine-simulation-persistence`` holds the one set of models of the
Public API's run tables, which the CI comparison checks against the tables the
Public API's migration chain builds. This library keeps only the comparison
path's own tables (``tables.py``) until that path is removed.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING
from uuid import uuid4

import policyengine_stage12_persistence
import pytest
from economy_output import (
    US_AT_LARGE_STATES,
    US_STATE_FIPS,
    uk_output_with_withheld_codes,
    us_output_with_every_part,
)
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_simulation_persistence import (
    HouseholdJobStatus,
    ReportRunStatus,
    SimulationStatus,
    engine,
    result_metadata,
    service_metadata,
)
from policyengine_simulation_persistence import tables as shared_tables
from policyengine_stage12_persistence import (
    DistrictCodes,
    budget_window_store,
    household_report_run_store,
    report_run_result,
    report_run_store,
    store,
)
from policyengine_stage12_persistence import engine as stage12_engine
from policyengine_stage12_persistence.report_run_store import (
    RUN_MODELS,
    RunKind,
    RunStatus,
)
from policyengine_stage12_persistence.tables import metadata as comparison_metadata
from sqlalchemy import Table

if TYPE_CHECKING:
    from collections.abc import Iterable


def _is_one_of(table: Table, tables: Iterable[Table]) -> bool:
    """Return whether ``table`` is one of ``tables`` itself, not just alike."""
    return any(table is candidate for candidate in tables)


def test_the_run_stores_write_the_shared_run_table_models() -> None:
    assert RUN_MODELS[RunKind.REPORT_RUN] is shared_tables.ReportRun
    assert RUN_MODELS[RunKind.SIMULATION_RUN] is shared_tables.SimulationRun
    assert RUN_MODELS[RunKind.HOUSEHOLD_JOB] is shared_tables.HouseholdJob
    assert report_run_store.BudgetWindowRun is shared_tables.BudgetWindowRun


def test_the_budget_window_store_writes_the_shared_run_table_models() -> None:
    assert budget_window_store.BudgetWindowRun is shared_tables.BudgetWindowRun
    assert budget_window_store.ReportRun is shared_tables.ReportRun
    assert budget_window_store.SimulationRun is shared_tables.SimulationRun


def test_the_household_report_store_writes_the_shared_run_table_models() -> None:
    assert household_report_run_store.ReportRun is shared_tables.ReportRun
    assert household_report_run_store.SimulationRun is shared_tables.SimulationRun


def test_every_table_the_library_names_is_shared_or_a_comparison_table() -> None:
    """The library declares no model of a Public API run or result table itself.

    Its only tables of its own are the comparison path's, in ``tables.py``.
    """
    shared_models = (
        *service_metadata.tables.values(),
        *result_metadata.tables.values(),
    )
    comparison_tables = tuple(comparison_metadata.tables.values())
    for module_info in pkgutil.iter_modules(policyengine_stage12_persistence.__path__):
        module = importlib.import_module(
            f"{policyengine_stage12_persistence.__name__}.{module_info.name}"
        )
        for name, value in vars(module).items():
            table = getattr(value, "__table__", value)
            if not isinstance(table, Table):
                continue
            assert _is_one_of(table, shared_models) or _is_one_of(
                table, comparison_tables
            ), f"{module_info.name}.{name} models {table.name} itself"


def test_every_result_row_goes_in_a_shared_result_table_model() -> None:
    run_ids = ReportRunIds(
        report_run_id=uuid4(),
        baseline_simulation_run_id=uuid4(),
        reform_simulation_run_id=uuid4(),
    )
    results = (
        report_run_result(
            us_output_with_every_part(),
            run_ids=run_ids,
            country="us",
            district_codes=DistrictCodes(
                state_fips=US_STATE_FIPS, at_large_states=US_AT_LARGE_STATES
            ),
        ),
        report_run_result(
            uk_output_with_withheld_codes(), run_ids=run_ids, country="uk"
        ),
    )
    result_models = tuple(result_metadata.tables.values())

    for result in results:
        for result_rows in result.rows:
            table = result_rows.model.__table__
            assert _is_one_of(table, result_models), table.name


def test_the_stores_connect_with_the_service_librarys_one_engine_helper() -> None:
    """One URL check and one unpooled engine serve every Modal call's store."""
    assert stage12_engine.create_unpooled_engine is engine.create_unpooled_engine
    assert stage12_engine.create_session_factory is engine.create_session_factory
    assert not hasattr(store, "normalized_database_url")
    for store_module in (store, report_run_store, budget_window_store):
        assert not hasattr(store_module, "create_unpooled_engine")


@pytest.mark.parametrize(
    "table_status", [SimulationStatus, ReportRunStatus, HouseholdJobStatus]
)
def test_run_status_has_each_run_tables_status_values(table_status) -> None:
    """``RunStatus`` is a status of any run kind, so it has each table's values.

    A read through the shared models returns the table's own enum member,
    which the stores convert with ``RunStatus(...)`` before comparing it.
    """
    assert [status.value for status in RunStatus] == [
        status.value for status in table_status
    ]
    for status in table_status:
        assert RunStatus(status) is RunStatus(status.value)
