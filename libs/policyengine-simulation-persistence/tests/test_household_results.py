"""A computed household stored as one JSON value and read back unchanged."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from policyengine_simulation_persistence import (
    HouseholdOutput,
    read_household_output,
    write_household_output,
)
from report_support import economy_pair, household_baseline
from run_support import economy_run, household_run
from simulation_support import household_simulation, simulation_store
from transition_support import clocked_stores

pytestmark = pytest.mark.postgres

SAMPLE = (
    Path(__file__).parent
    / "fixtures"
    / "household_outputs"
    / ("us_household_2026.json")
)


def _sample() -> HouseholdOutput:
    return HouseholdOutput.model_validate_json(SAMPLE.read_text(encoding="utf-8"))


def _running_household_pair(stores, database, rows):
    baseline = household_baseline(database, rows)
    reform = simulation_store(database).create(
        household_simulation(rows, policy_id=rows.us_policy_id)
    )
    runs = [
        stores.runs.start_simulation_run(household_run(simulation, rows)).run
        for simulation in (baseline, reform)
    ]
    run_ids = [run.id for run in runs]
    stores.transitions.claim_simulation_dispatch(run_ids)
    stores.transitions.record_simulation_dispatched(
        run_ids, modal_call_id="fc-household", first_check_at=stores.clock()
    )
    return runs


def test_a_household_pair_is_stored_with_its_success_and_read_back_unchanged(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    runs = _running_household_pair(stores, migrated_database, public_api_rows)
    output = _sample()

    def write_both(connection, succeeded_runs) -> None:
        for run in succeeded_runs:
            write_household_output(connection, run, output)

    assert stores.transitions.complete_simulation_call("fc-household", write_both)

    with stores.transitions.engine.connect() as connection:
        for run in runs:
            stored = read_household_output(connection, run.id)
            assert stored.model_dump_json() == output.model_dump_json()
            assert list(stored.household) == list(
                json.loads(SAMPLE.read_text())["household"]
            )


def test_a_failed_household_write_records_no_success(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    runs = _running_household_pair(stores, migrated_database, public_api_rows)
    broken = HouseholdOutput(schema_version=1, household={"people": math.nan})

    def write_both(connection, succeeded_runs) -> None:
        for run in succeeded_runs:
            write_household_output(connection, run, broken)

    with pytest.raises(ValueError, match="NaN"):
        stores.transitions.complete_simulation_call("fc-household", write_both)

    assert {stores.runs.get_simulation_run(run.id).status.value for run in runs} == {
        "running"
    }
    with (
        stores.transitions.engine.connect() as connection,
        pytest.raises(LookupError, match="no computed household"),
    ):
        read_household_output(connection, runs[0].id)


def test_only_a_household_run_has_a_computed_household(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    baseline, _ = economy_pair(migrated_database, public_api_rows)
    run = stores.runs.start_simulation_run(economy_run(baseline, public_api_rows)).run

    with (
        stores.transitions.engine.begin() as connection,
        pytest.raises(ValueError, match="household simulation run"),
    ):
        write_household_output(connection, run, _sample())


def test_a_household_output_is_a_json_object_with_a_schema_version() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        HouseholdOutput(schema_version=0, household={})
    with pytest.raises(ValueError, match="household"):
        HouseholdOutput.model_validate({"schema_version": 1, "household": ["people"]})
