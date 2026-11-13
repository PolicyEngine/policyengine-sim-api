"""The Modal worker role's writes to a household report run and its simulation runs.

A household function given a household report run claims the report run and
each of its simulation runs under its Modal call ID, records each simulation
run's calculated household, or its failure, in ``household_result``, then
marks the report run succeeded or failed. These run against a disposable
Postgres that the Public API's v2 migrations created, connected as the Modal
worker role (see ``report_run_database``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from household_report_database import (
    remove_household_report_run,
    seed_household_report_run,
)
from policyengine_simulation_contract.household_execution import (
    CalculatedHousehold,
    HouseholdBundleProvenance,
    HouseholdCalculationError,
    HouseholdCalculationOutput,
    HouseholdExecutionFailure,
    HouseholdExecutionResult,
    HouseholdWorkerIdentity,
    parse_household_execution_outcome,
)
from policyengine_stage12_persistence import (
    HouseholdReportRunStore,
    RunStatus,
    RunWrite,
)
from report_run_database import NOW, worker_database_url
from sqlalchemy import select

if TYPE_CHECKING:
    from collections.abc import Iterator

    from household_report_database import SeededHouseholdReportRun
    from report_run_database import ReportRunSeeder

LATER = NOW + timedelta(minutes=5)
CALL = "fc-household-report"
OTHER_CALL = "fc-other"
WORKER = HouseholdWorkerIdentity(
    application_name="policyengine-simulation-v2-py6-12-0",
    function_name="calculate_household_us",
)
CALCULATION_ID = UUID("00000000-0000-4000-8000-0000000000c1")
# The documents a household function stores, built from the contract's models
# so they stay valid household function outcomes.
SUCCEEDED_DOCUMENT = HouseholdExecutionResult(
    calculation_id=CALCULATION_ID,
    output=HouseholdCalculationOutput(
        country="us",
        bundle=HouseholdBundleProvenance(
            policyengine_version="6.12.0",
            core_package_version="3.33.0",
            country_package_name="policyengine-us",
            country_package_version="1.0.0",
            bundle_manifest_sha256="a" * 64,
        ),
        household=CalculatedHousehold.model_validate(
            {
                "people": {"you": {"age": {"2026": 40}}},
                "households": {
                    "your household": {
                        "members": ["you"],
                        "household_net_income": {"2026": 25_000.0},
                    }
                },
            }
        ),
    ),
    worker=WORKER,
).model_dump(mode="json")
FAILED_DOCUMENT = HouseholdExecutionFailure(
    calculation_id=CALCULATION_ID,
    error=HouseholdCalculationError(
        code="invalid_inputs",
        message="Unrecognized calculate input(s): Unrecognized policy parameter",
    ),
    worker=WORKER,
).model_dump(mode="json")
FAILURE_SUMMARY = (
    "invalid_inputs: Unrecognized calculate input(s): Unrecognized policy parameter"
)


@pytest.fixture
def store() -> Iterator[HouseholdReportRunStore]:
    run_store = HouseholdReportRunStore(worker_database_url())
    try:
        yield run_store
    finally:
        run_store.engine.dispose()


@pytest.fixture
def seeded_report(seeder: ReportRunSeeder) -> Iterator[SeededHouseholdReportRun]:
    seeded = seed_household_report_run(seeder)
    try:
        yield seeded
    finally:
        remove_household_report_run(seeder, seeded)


@pytest.fixture
def seeded_baseline_only(
    seeder: ReportRunSeeder,
) -> Iterator[SeededHouseholdReportRun]:
    seeded = seed_household_report_run(seeder, with_reform=False)
    try:
        yield seeded
    finally:
        remove_household_report_run(seeder, seeded)


def _result_is_null(seeder: ReportRunSeeder, simulation_run_id: UUID) -> bool:
    """Whether the run's household_result is SQL NULL, not a stored JSON null."""
    runs = seeder.table("simulation_runs")
    with seeder.engine.connect() as connection:
        return (
            connection.execute(
                select(runs.c.id).where(
                    runs.c.id == simulation_run_id, runs.c.household_result.is_(None)
                )
            ).one_or_none()
            is not None
        )


def test_a_household_report_runs_simulation_runs_are_read(
    store: HouseholdReportRunStore,
    seeded_report: SeededHouseholdReportRun,
    seeded_baseline_only: SeededHouseholdReportRun,
) -> None:
    both = store.household_report_simulation_runs(seeded_report.report_run_id)
    baseline_only = store.household_report_simulation_runs(
        seeded_baseline_only.report_run_id
    )

    assert both == (
        seeded_report.baseline_simulation_run_id,
        seeded_report.reform_simulation_run_id,
    )
    assert baseline_only == (seeded_baseline_only.baseline_simulation_run_id, None)
    missing_run_id = uuid4()
    with pytest.raises(LookupError, match=f"report run {missing_run_id}"):
        store.household_report_simulation_runs(missing_run_id)


def test_a_household_simulation_run_records_its_result_only_while_its_call_holds_it(
    store: HouseholdReportRunStore,
    seeder: ReportRunSeeder,
    seeded_report: SeededHouseholdReportRun,
) -> None:
    run_id = seeded_report.baseline_simulation_run_id
    store.claim_simulation_run(run_id, modal_call_id=CALL, claimed_at=NOW)

    by_other_call = store.complete_household_simulation_run(
        run_id,
        modal_call_id=OTHER_CALL,
        household_result=SUCCEEDED_DOCUMENT,
        completed_at=LATER,
    )
    completed = store.complete_household_simulation_run(
        run_id,
        modal_call_id=CALL,
        household_result=SUCCEEDED_DOCUMENT,
        completed_at=LATER,
    )
    again = store.complete_household_simulation_run(
        run_id,
        modal_call_id=CALL,
        household_result=FAILED_DOCUMENT,
        completed_at=LATER,
    )

    assert by_other_call == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert completed == RunWrite(changed=True, status=RunStatus.SUCCEEDED)
    assert again == RunWrite(changed=False, status=RunStatus.SUCCEEDED)
    row = seeder.row("simulation_runs", run_id)
    assert row["status"] == "succeeded"
    assert row["household_result"] == SUCCEEDED_DOCUMENT
    assert parse_household_execution_outcome(row["household_result"]).status == (
        "succeeded"
    )
    assert row["completed_at"] == LATER
    assert row["error_message"] is None
    assert (row["output_uri"], row["output_sha256"]) == (None, None)


def test_a_household_simulation_run_fails_with_its_failure_document(
    store: HouseholdReportRunStore,
    seeder: ReportRunSeeder,
    seeded_report: SeededHouseholdReportRun,
) -> None:
    run_id = seeded_report.reform_simulation_run_id
    assert run_id is not None
    store.claim_simulation_run(run_id, modal_call_id=CALL, claimed_at=NOW)

    by_other_call = store.fail_household_simulation_run(
        run_id,
        modal_call_id=OTHER_CALL,
        error_message=FAILURE_SUMMARY,
        failed_at=LATER,
        household_result=FAILED_DOCUMENT,
    )
    failed = store.fail_household_simulation_run(
        run_id,
        modal_call_id=CALL,
        error_message=FAILURE_SUMMARY,
        failed_at=LATER,
        household_result=FAILED_DOCUMENT,
    )
    completed_after = store.complete_household_simulation_run(
        run_id,
        modal_call_id=CALL,
        household_result=SUCCEEDED_DOCUMENT,
        completed_at=LATER,
    )

    assert by_other_call == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    assert completed_after == RunWrite(changed=False, status=RunStatus.FAILED)
    row = seeder.row("simulation_runs", run_id)
    assert row["household_result"] == FAILED_DOCUMENT
    assert row["error_message"] == FAILURE_SUMMARY
    assert row["completed_at"] == LATER


def test_a_household_simulation_run_failed_without_a_document_has_no_result(
    store: HouseholdReportRunStore,
    seeder: ReportRunSeeder,
    seeded_report: SeededHouseholdReportRun,
) -> None:
    run_id = seeded_report.baseline_simulation_run_id
    store.claim_simulation_run(run_id, modal_call_id=CALL, claimed_at=NOW)

    failed = store.fail_household_simulation_run(
        run_id,
        modal_call_id=CALL,
        error_message="household_function_error: The household function raised X.",
        failed_at=LATER,
    )

    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    assert _result_is_null(seeder, run_id)


def test_a_household_report_run_completes_only_while_its_call_holds_it(
    store: HouseholdReportRunStore,
    seeder: ReportRunSeeder,
    seeded_baseline_only: SeededHouseholdReportRun,
) -> None:
    report_run_id = seeded_baseline_only.report_run_id
    claimed = store.claim_report_run(report_run_id, modal_call_id=CALL, claimed_at=NOW)

    by_other_call = store.complete_household_report_run(
        report_run_id, modal_call_id=OTHER_CALL, completed_at=LATER
    )
    completed = store.complete_household_report_run(
        report_run_id, modal_call_id=CALL, completed_at=LATER
    )
    failed_after = store.fail_report_run(
        report_run_id,
        modal_call_id=CALL,
        error_message="household_calculation_failed: late",
        failed_at=LATER,
    )

    assert claimed == RunWrite(changed=True, status=RunStatus.RUNNING)
    assert by_other_call == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert completed == RunWrite(changed=True, status=RunStatus.SUCCEEDED)
    assert failed_after == RunWrite(changed=False, status=RunStatus.SUCCEEDED)
    row = seeder.row("report_runs", report_run_id)
    assert row["completed_at"] == LATER
    assert row["error_message"] is None
    assert row["baseline_cliff_gap"] is None
    assert row["spm_provenance"] is None


def test_completing_a_missing_simulation_run_raises_lookup_error(
    store: HouseholdReportRunStore,
) -> None:
    missing_run_id = uuid4()

    with pytest.raises(LookupError, match=f"simulation run {missing_run_id}"):
        store.complete_household_simulation_run(
            missing_run_id,
            modal_call_id=CALL,
            household_result=SUCCEEDED_DOCUMENT,
            completed_at=LATER,
        )
