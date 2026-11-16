"""The Modal worker role's stores use the simulation service's table models.

``policyengine-simulation-persistence`` holds the one set of models of the
Public API's run tables, which the CI comparison checks against the tables the
Public API's migration chain builds. This library keeps only the comparison
path's own tables (``tables.py``) until that path is removed.
"""

from __future__ import annotations

from policyengine_simulation_persistence import tables as shared_tables
from policyengine_stage12_persistence import (
    budget_window_store,
    household_report_run_store,
    report_run_store,
)
from policyengine_stage12_persistence.report_run_store import RUN_TABLES, RunKind


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
