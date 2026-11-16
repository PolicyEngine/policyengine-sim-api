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
from economy_output import (
    US_AT_LARGE_STATES,
    US_STATE_FIPS,
    uk_output_with_withheld_codes,
    us_output_with_every_part,
)
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_simulation_persistence import (
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
from policyengine_stage12_persistence.report_run_store import RUN_TABLES, RunKind
from policyengine_stage12_persistence.tables import metadata as comparison_metadata
from sqlalchemy import Table

if TYPE_CHECKING:
    from collections.abc import Iterable


def _is_one_of(table: Table, tables: Iterable[Table]) -> bool:
    """Return whether ``table`` is one of ``tables`` itself, not just alike."""
    return any(table is candidate for candidate in tables)


def test_the_run_stores_write_the_shared_run_table_models() -> None:
    assert RUN_TABLES[RunKind.REPORT_RUN] is shared_tables.report_runs
    assert RUN_TABLES[RunKind.SIMULATION_RUN] is shared_tables.simulation_runs
    assert RUN_TABLES[RunKind.HOUSEHOLD_JOB] is shared_tables.household_jobs
    assert report_run_store.budget_window_runs is shared_tables.budget_window_runs


def test_the_budget_window_store_writes_the_shared_run_table_models() -> None:
    assert budget_window_store.budget_window_runs is shared_tables.budget_window_runs
    assert budget_window_store.report_runs is shared_tables.report_runs
    assert budget_window_store.simulation_runs is shared_tables.simulation_runs


def test_the_household_report_store_writes_the_shared_run_table_models() -> None:
    assert household_report_run_store.report_runs is shared_tables.report_runs
    assert household_report_run_store.simulation_runs is shared_tables.simulation_runs


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
            if not isinstance(value, Table):
                continue
            assert _is_one_of(value, shared_models) or _is_one_of(
                value, comparison_tables
            ), f"{module_info.name}.{name} models {value.name} itself"


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
            assert _is_one_of(result_rows.table, result_models), result_rows.table.name


def test_the_stores_connect_with_the_service_librarys_one_engine_helper() -> None:
    """One URL check and one unpooled engine serve every Modal call's store."""
    assert store.create_stage12_engine is engine.create_unpooled_engine
    assert not hasattr(store, "normalized_database_url")
    assert report_run_store.create_stage12_engine is engine.create_unpooled_engine
    assert budget_window_store.create_stage12_engine is engine.create_unpooled_engine
