"""The Modal worker role's conditional writes to household jobs.

A household function claims the job it is given under its Modal call ID,
then records the calculation's result or failure; the stuck-run sweep lists
and fails household jobs nothing will finish. These run against a disposable
Postgres that the Public API's v2 migrations created, connected as the Modal
worker role (see ``report_run_database``).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
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
    ActiveRun,
    ReportRunStore,
    RunKind,
    RunStatus,
    RunWrite,
)
from report_run_database import NOW, SeededHouseholdJob, worker_database_url
from sqlalchemy import select

LATER = NOW + timedelta(minutes=5)
CUTOFF = NOW + timedelta(minutes=30)
CALCULATION_ID = UUID("00000000-0000-4000-8000-0000000000c1")
WORKER = HouseholdWorkerIdentity(
    application_name="policyengine-simulation-v2-py6-12-0",
    function_name="calculate_household_us",
)
# The documents a household function stores, built from the contract's models
# so they stay valid household function outcomes.
SUCCEEDED_OUTCOME = HouseholdExecutionResult(
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
)
FAILED_OUTCOME = HouseholdExecutionFailure(
    calculation_id=CALCULATION_ID,
    error=HouseholdCalculationError(
        code="invalid_inputs",
        message="Unrecognized calculate input(s): Unrecognized policy parameter",
    ),
    worker=WORKER,
)
SUCCEEDED_DOCUMENT = SUCCEEDED_OUTCOME.model_dump(mode="json")
FAILED_DOCUMENT = FAILED_OUTCOME.model_dump(mode="json")
FAILURE_SUMMARY = "invalid_inputs: Unrecognized calculate input(s): ..."
ENDED_MESSAGE = "modal_call_ended: the job's Modal call ended without recording it"


@pytest.fixture
def store():
    run_store = ReportRunStore(worker_database_url())
    try:
        yield run_store
    finally:
        run_store.engine.dispose()


def _set_job(seeder, job_id, **values):
    jobs = seeder.table("household_jobs")
    with seeder.engine.begin() as connection:
        connection.execute(jobs.update().where(jobs.c.id == job_id).values(**values))


def _result_is_null(seeder, job_id) -> bool:
    """Whether the job's result is SQL NULL, not a stored JSON null."""
    jobs = seeder.table("household_jobs")
    with seeder.engine.connect() as connection:
        return (
            connection.execute(
                select(jobs.c.id).where(jobs.c.id == job_id, jobs.c.result.is_(None))
            ).one_or_none()
            is not None
        )


def _listed_jobs(store, seeded_household_job: SeededHouseholdJob, cutoff=CUTOFF):
    return {
        job
        for job in store.list_active_runs(
            RunKind.HOUSEHOLD_JOB, active_since_before=cutoff
        )
        if job.run_id == seeded_household_job.job_id
    }


def test_a_household_job_is_claimed_once_and_resumed_by_the_same_call(
    store, seeder, seeded_household_job: SeededHouseholdJob
) -> None:
    job_id = seeded_household_job.job_id

    first = store.claim_household_job(
        job_id, modal_call_id="fc-household", claimed_at=NOW
    )
    other_call = store.claim_household_job(
        job_id, modal_call_id="fc-other", claimed_at=LATER
    )
    # Modal restarts a pre-empted call under the same call ID.
    restarted = store.claim_household_job(
        job_id, modal_call_id="fc-household", claimed_at=LATER
    )

    assert first == RunWrite(changed=True, status=RunStatus.RUNNING)
    assert other_call == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert restarted == RunWrite(changed=True, status=RunStatus.RUNNING)
    row = seeder.row("household_jobs", job_id)
    assert row["modal_call_id"] == "fc-household"
    assert row["started_at"] == NOW
    assert row["updated_at"] == LATER


def test_a_pending_household_job_is_claimed_only_by_the_call_recorded_on_it(
    store, seeder, seeded_household_job: SeededHouseholdJob
) -> None:
    # The simulation service records the call it started while the job is
    # still pending; no other call may take the job.
    job_id = seeded_household_job.job_id
    _set_job(seeder, job_id, modal_call_id="fc-household")

    other_call = store.claim_household_job(
        job_id, modal_call_id="fc-other", claimed_at=NOW
    )
    recorded_call = store.claim_household_job(
        job_id, modal_call_id="fc-household", claimed_at=LATER
    )

    assert other_call == RunWrite(changed=False, status=RunStatus.PENDING)
    assert recorded_call == RunWrite(changed=True, status=RunStatus.RUNNING)
    row = seeder.row("household_jobs", job_id)
    assert row["modal_call_id"] == "fc-household"
    assert row["started_at"] == LATER


def test_a_household_job_completes_with_its_result_only_while_its_call_holds_it(
    store, seeder, seeded_household_job: SeededHouseholdJob
) -> None:
    job_id = seeded_household_job.job_id
    unclaimed = store.complete_household_job(
        job_id,
        modal_call_id="fc-household",
        result=SUCCEEDED_DOCUMENT,
        completed_at=NOW,
    )
    store.claim_household_job(job_id, modal_call_id="fc-household", claimed_at=NOW)

    by_other_call = store.complete_household_job(
        job_id, modal_call_id="fc-other", result=SUCCEEDED_DOCUMENT, completed_at=LATER
    )
    completed = store.complete_household_job(
        job_id,
        modal_call_id="fc-household",
        result=SUCCEEDED_DOCUMENT,
        completed_at=LATER,
    )
    repeated = store.complete_household_job(
        job_id,
        modal_call_id="fc-household",
        result=SUCCEEDED_OUTCOME.model_copy(
            update={"calculation_id": uuid4()}
        ).model_dump(mode="json"),
        completed_at=CUTOFF,
    )
    failed_after = store.fail_household_job(
        job_id,
        modal_call_id="fc-household",
        error_message=FAILURE_SUMMARY,
        result=FAILED_DOCUMENT,
        failed_at=CUTOFF,
    )
    reclaimed = store.claim_household_job(
        job_id, modal_call_id="fc-household", claimed_at=CUTOFF
    )

    assert unclaimed == RunWrite(changed=False, status=RunStatus.PENDING)
    assert by_other_call == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert completed == RunWrite(changed=True, status=RunStatus.SUCCEEDED)
    assert repeated == RunWrite(changed=False, status=RunStatus.SUCCEEDED)
    assert failed_after == RunWrite(changed=False, status=RunStatus.SUCCEEDED)
    assert reclaimed == RunWrite(changed=False, status=RunStatus.SUCCEEDED)
    row = seeder.row("household_jobs", job_id)
    assert row["status"] == "succeeded"
    assert row["result"] == SUCCEEDED_DOCUMENT
    assert parse_household_execution_outcome(row["result"]) == SUCCEEDED_OUTCOME
    assert row["error_message"] is None
    assert row["completed_at"] == LATER
    assert row["updated_at"] == LATER


def test_a_household_job_fails_with_its_failure_document_and_then_never_completes(
    store, seeder, seeded_household_job: SeededHouseholdJob
) -> None:
    job_id = seeded_household_job.job_id
    unclaimed = store.fail_household_job(
        job_id,
        modal_call_id="fc-household",
        error_message=FAILURE_SUMMARY,
        result=FAILED_DOCUMENT,
        failed_at=NOW,
    )
    store.claim_household_job(job_id, modal_call_id="fc-household", claimed_at=NOW)

    by_other_call = store.fail_household_job(
        job_id,
        modal_call_id="fc-other",
        error_message="another failure",
        failed_at=LATER,
    )
    failed = store.fail_household_job(
        job_id,
        modal_call_id="fc-household",
        error_message=FAILURE_SUMMARY,
        result=FAILED_DOCUMENT,
        failed_at=LATER,
    )
    again = store.fail_household_job(
        job_id,
        modal_call_id="fc-household",
        error_message="another failure",
        failed_at=CUTOFF,
    )
    completed = store.complete_household_job(
        job_id,
        modal_call_id="fc-household",
        result=SUCCEEDED_DOCUMENT,
        completed_at=CUTOFF,
    )

    assert unclaimed == RunWrite(changed=False, status=RunStatus.PENDING)
    assert by_other_call == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    assert again == RunWrite(changed=False, status=RunStatus.FAILED)
    assert completed == RunWrite(changed=False, status=RunStatus.FAILED)
    row = seeder.row("household_jobs", job_id)
    assert row["result"] == FAILED_DOCUMENT
    assert parse_household_execution_outcome(row["result"]) == FAILED_OUTCOME
    assert row["error_message"] == FAILURE_SUMMARY
    assert row["completed_at"] == LATER


def test_a_household_job_failed_without_a_failure_document_has_no_result(
    store, seeder, seeded_household_job: SeededHouseholdJob
) -> None:
    # An error outside the calculation leaves no failure document; the job
    # records only its summary, and its result stays SQL NULL.
    job_id = seeded_household_job.job_id
    store.claim_household_job(job_id, modal_call_id="fc-household", claimed_at=NOW)

    failed = store.fail_household_job(
        job_id,
        modal_call_id="fc-household",
        error_message="household_calculation_error: the calculation raised",
        failed_at=LATER,
    )

    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    assert _result_is_null(seeder, job_id)
    row = seeder.row("household_jobs", job_id)
    assert row["error_message"] == "household_calculation_error: the calculation raised"


def test_a_missing_household_job_raises_lookup_error(store) -> None:
    with pytest.raises(LookupError, match="household job"):
        store.claim_household_job(uuid4(), modal_call_id="fc-household", claimed_at=NOW)


def test_the_sweep_lists_household_jobs_counted_from_their_creation(
    store, seeder, seeded_household_job: SeededHouseholdJob
) -> None:
    job_id = seeded_household_job.job_id
    _set_job(seeder, job_id, created_at=NOW)

    assert _listed_jobs(store, seeded_household_job) == {
        ActiveRun(
            kind=RunKind.HOUSEHOLD_JOB,
            run_id=job_id,
            status=RunStatus.PENDING,
            modal_call_id=None,
        )
    }
    assert _listed_jobs(store, seeded_household_job, NOW) == set()

    # A job claimed after the cutoff still counts from its creation.
    store.claim_household_job(
        job_id, modal_call_id="fc-household", claimed_at=CUTOFF + timedelta(hours=1)
    )
    assert _listed_jobs(store, seeded_household_job) == {
        ActiveRun(
            kind=RunKind.HOUSEHOLD_JOB,
            run_id=job_id,
            status=RunStatus.RUNNING,
            modal_call_id="fc-household",
        )
    }

    _set_job(seeder, job_id, status="succeeded", completed_at=CUTOFF)
    assert _listed_jobs(store, seeded_household_job) == set()


def test_the_sweep_fails_a_household_job_only_while_it_is_as_listed(
    store, seeder, seeded_household_job: SeededHouseholdJob
) -> None:
    job_id = seeded_household_job.job_id
    _set_job(seeder, job_id, created_at=NOW)
    (listed_without_call,) = _listed_jobs(store, seeded_household_job)
    # After the listing, the simulation service recorded the call it started.
    _set_job(seeder, job_id, modal_call_id="fc-household")
    late_call = store.fail_stuck_run(
        listed_without_call, error_message=ENDED_MESSAGE, failed_at=CUTOFF
    )
    (listed_with_call,) = _listed_jobs(store, seeded_household_job)

    first = store.fail_stuck_run(
        listed_with_call, error_message=ENDED_MESSAGE, failed_at=CUTOFF
    )
    again = store.fail_stuck_run(
        listed_with_call, error_message=ENDED_MESSAGE, failed_at=CUTOFF
    )

    assert late_call == RunWrite(changed=False, status=RunStatus.PENDING)
    assert first == RunWrite(changed=True, status=RunStatus.FAILED)
    assert again == RunWrite(changed=False, status=RunStatus.FAILED)
    row = seeder.row("household_jobs", job_id)
    assert row["error_message"] == ENDED_MESSAGE
    assert row["completed_at"] == CUTOFF
    assert row["updated_at"] == CUTOFF
    assert _result_is_null(seeder, job_id)
