"""A failure may carry a detail document, stored with the run it failed."""

from __future__ import annotations

import pytest
from policyengine_simulation_persistence import (
    NewReportRun,
    RunError,
    RunErrorCode,
    RunStatus,
    RunTrigger,
)
from public_api_support import superuser_engine
from report_support import economy_pair, economy_report, household_baseline
from report_support import report_store as definition_report_store
from run_support import economy_run, household_run
from simulation_support import household_simulation, simulation_store
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from transition_support import clocked_stores

pytestmark = pytest.mark.postgres

INPUT_ERRORS = {
    "code": "invalid_inputs",
    "message": "Unrecognized inputs: employment_incme",
    "input_errors": [
        {
            "type": "household_variable",
            "name": "employment_incme",
            "path": "people/you/employment_incme",
            "message": "employment_incme is not a variable",
        }
    ],
}
REJECTED = RunError(
    RunErrorCode.WORKER_ERROR,
    "The household calculation failed: invalid_inputs.",
    detail=INPUT_ERRORS,
)


def _running_household_pair(stores, database, rows, call_id="fc-household"):
    baseline = household_baseline(database, rows)
    reform = simulation_store(database).create(
        household_simulation(rows, policy_id=rows.us_policy_id)
    )
    run_ids = [
        stores.runs.start_simulation_run(household_run(simulation, rows)).run.id
        for simulation in (baseline, reform)
    ]
    assert stores.transitions.claim_simulation_dispatch(run_ids)
    assert stores.transitions.record_simulation_dispatched(
        run_ids, modal_call_id=call_id, first_check_at=stores.clock()
    )
    return run_ids


def test_a_failed_call_records_its_detail_on_every_run_it_computes(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    run_ids = _running_household_pair(stores, migrated_database, public_api_rows)

    assert stores.transitions.fail_simulation_call("fc-household", REJECTED)

    for run_id in run_ids:
        failed = stores.runs.get_simulation_run(run_id)
        assert failed.status is RunStatus.FAILED
        assert failed.error_message == REJECTED.message
        assert failed.error_detail == INPUT_ERRORS


def test_a_failure_without_detail_records_none(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    run_ids = _running_household_pair(stores, migrated_database, public_api_rows)

    assert stores.transitions.fail_simulation_call(
        "fc-household", RunError(RunErrorCode.WORKER_TIMEOUT, "The call timed out.")
    )

    assert stores.runs.get_simulation_run(run_ids[0]).error_detail is None


def test_a_refused_dispatch_records_its_detail(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    run = stores.runs.start_simulation_run(
        household_run(
            household_baseline(migrated_database, public_api_rows), public_api_rows
        )
    ).run
    stores.transitions.claim_simulation_dispatch([run.id])
    refused = RunError(
        RunErrorCode.DISPATCH_FAILED,
        "Starting the Modal call raised NotFoundError.",
        detail={"modal_function": "calculate_household_us"},
    )

    assert stores.transitions.record_simulation_dispatch_failure([run.id], refused)

    assert stores.runs.get_simulation_run(run.id).error_detail == {
        "modal_function": "calculate_household_us"
    }


def test_a_failed_comparison_records_its_detail(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    baseline, reform = economy_pair(migrated_database, public_api_rows)
    report = definition_report_store(migrated_database).create(
        economy_report(baseline, reform)
    )
    runs = [
        stores.runs.start_simulation_run(economy_run(simulation, public_api_rows)).run
        for simulation in (baseline, reform)
    ]
    report_run = stores.runs.create_report_run(
        NewReportRun(
            report_id=report.id,
            baseline_simulation_run_id=runs[0].id,
            reform_simulation_run_id=runs[1].id,
            trigger=RunTrigger.INITIAL,
        )
    )
    for run in runs:
        call_id = f"fc-{run.id.hex[:8]}"
        stores.transitions.claim_simulation_dispatch([run.id])
        stores.transitions.record_simulation_dispatched(
            [run.id], modal_call_id=call_id, first_check_at=stores.clock()
        )
        stores.transitions.complete_simulation_call(call_id, lambda *_: None)
    stores.transitions.claim_comparison_dispatch(report_run.id)
    stores.transitions.record_comparison_dispatched(
        report_run.id, modal_call_id="fc-comparison", first_check_at=stores.clock()
    )

    assert stores.transitions.fail_comparison(
        report_run.id,
        RunError(
            RunErrorCode.WORKER_ERROR,
            "The Modal call raised ReportExecutionError.",
            detail={"code": "missing_parameter"},
        ),
    )

    failed = stores.runs.get_report_run(report_run.id)
    assert failed.comparison_error_detail == {"code": "missing_parameter"}


def test_the_database_keeps_details_for_failed_runs_only(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    run_ids = _running_household_pair(stores, migrated_database, public_api_rows)
    engine = superuser_engine(migrated_database.superuser_url)
    try:
        with (
            pytest.raises(IntegrityError, match="ck_simulation_runs_error_detail"),
            engine.begin() as connection,
        ):
            connection.execute(
                text(
                    "UPDATE simulation.simulation_runs "
                    "SET error_detail = '{}'::jsonb WHERE id = :run_id"
                ),
                {"run_id": run_ids[0]},
            )
    finally:
        engine.dispose()


def test_an_error_detail_must_be_a_json_object() -> None:
    with pytest.raises(TypeError, match="detail must be a JSON object"):
        RunError(RunErrorCode.WORKER_ERROR, "x", detail=["invalid_inputs"])  # type: ignore[arg-type]
