"""The rows of a report run that names one simulation run on both sides.

A report comparing a simulation with itself names that simulation's one run
as its baseline and its reform. The run's own results, its poverty and
inequality, are then one set of rows, as for any other run, not one per side.
"""

from __future__ import annotations

from uuid import UUID

from economy_output import US_ECONOMY_OUTPUT, uk_economy_output
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_stage12_persistence.report_run_results import (
    inequality,
    poverty,
    report_run_result,
)

SIMULATION_RUN_ID = UUID("00000000-0000-0000-0000-00000000000b")
ONE_RUN_ON_BOTH_SIDES = ReportRunIds(
    report_run_id=UUID("00000000-0000-0000-0000-00000000000a"),
    baseline_simulation_run_id=SIMULATION_RUN_ID,
    reform_simulation_run_id=SIMULATION_RUN_ID,
)
TWO_RUNS = ReportRunIds(
    report_run_id=UUID("00000000-0000-0000-0000-00000000000a"),
    baseline_simulation_run_id=SIMULATION_RUN_ID,
    reform_simulation_run_id=UUID("00000000-0000-0000-0000-00000000000c"),
)


def _run_rows(output, run_ids: ReportRunIds, country: str):
    """Return the poverty and inequality rows of ``output``, without their IDs."""
    result = report_run_result(output, run_ids=run_ids, country=country)
    rows_by_table = {
        rows.table.name: [
            {name: value for name, value in row.items() if name != "id"}
            for row in rows.rows
        ]
        for rows in result.rows
        if rows.table in {poverty, inequality}
    }
    return rows_by_table["poverty"], rows_by_table["inequality"]


def test_one_run_on_both_sides_stores_its_poverty_and_inequality_once() -> None:
    poverty_rows, inequality_rows = _run_rows(
        US_ECONOMY_OUTPUT, ONE_RUN_ON_BOTH_SIDES, "us"
    )
    two_runs_poverty, two_runs_inequality = _run_rows(US_ECONOMY_OUTPUT, TWO_RUNS, "us")

    # The one run's rows are the baseline's rows of a report with two runs.
    assert poverty_rows == [
        row for row in two_runs_poverty if row["simulation_run_id"] == SIMULATION_RUN_ID
    ]
    assert inequality_rows == [
        row
        for row in two_runs_inequality
        if row["simulation_run_id"] == SIMULATION_RUN_ID
    ]
    assert len(inequality_rows) == 1


def test_one_run_on_both_sides_stores_one_set_in_the_uk_too() -> None:
    poverty_rows, inequality_rows = _run_rows(
        uk_economy_output(), ONE_RUN_ON_BOTH_SIDES, "uk"
    )
    keys = [
        (row["poverty_type"], row["filter_variable"], row["filter_group"])
        for row in poverty_rows
    ]

    assert len(keys) == len(set(keys))
    assert len(inequality_rows) == 1
