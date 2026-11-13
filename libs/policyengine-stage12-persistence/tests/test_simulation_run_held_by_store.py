"""Failing a simulation run whose holder's call ended without recording an outcome.

A report run's coordinator that waits for a simulation run another call holds,
and sees that call end with the run still unfinished, fails the run, but only
while it still records that call: another coordinator may already have
failed it, or the run may have finished meanwhile.

These run against a disposable Postgres that the Public API's v2 migrations
created, connected as the Modal worker role (see ``report_run_database``).
Each seeded run is of its own simulation, so no two unfailed runs share a
simulation and version set.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from policyengine_stage12_persistence import ReportRunStore, RunStatus, RunWrite
from report_run_database import NOW, SeededReportRun, worker_database_url
from sqlalchemy import update

LATER = NOW + timedelta(minutes=5)
ENDED_CALL_MESSAGE = "modal_call_ended: the holder's call ended."


@pytest.fixture
def store():
    run_store = ReportRunStore(worker_database_url())
    try:
        yield run_store
    finally:
        run_store.engine.dispose()


def _recorded_call(seeder, run_id, modal_call_id: str) -> None:
    """Record a call on a pending run, as the simulation service does at its start."""
    with seeder.engine.begin() as connection:
        connection.execute(
            update(seeder.table("simulation_runs"))
            .where(seeder.table("simulation_runs").c.id == run_id)
            .values(modal_call_id=modal_call_id)
        )


def test_a_running_run_is_failed_only_while_it_records_the_ended_call(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    run_id = seeded_run.run_ids.baseline_simulation_run_id
    store.claim_simulation_run(run_id, modal_call_id="fc-holder", claimed_at=NOW)

    by_other_call = store.fail_simulation_run_held_by(
        run_id,
        modal_call_id="fc-other",
        error_message=ENDED_CALL_MESSAGE,
        failed_at=LATER,
    )
    failed = store.fail_simulation_run_held_by(
        run_id,
        modal_call_id="fc-holder",
        error_message=ENDED_CALL_MESSAGE,
        failed_at=LATER,
    )
    repeated = store.fail_simulation_run_held_by(
        run_id,
        modal_call_id="fc-holder",
        error_message="modal_call_ended: a second waiter",
        failed_at=LATER,
    )

    assert by_other_call == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    assert repeated == RunWrite(changed=False, status=RunStatus.FAILED)
    row = seeder.row("simulation_runs", run_id)
    assert row["error_message"] == ENDED_CALL_MESSAGE
    assert row["completed_at"] == LATER


def test_a_pending_run_under_a_call_that_never_claimed_it_is_failed(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    run_id = seeded_run.run_ids.reform_simulation_run_id
    _recorded_call(seeder, run_id, "fc-service-started")

    failed = store.fail_simulation_run_held_by(
        run_id,
        modal_call_id="fc-service-started",
        error_message=ENDED_CALL_MESSAGE,
        failed_at=LATER,
    )

    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    assert seeder.row("simulation_runs", run_id)["error_message"] == (
        ENDED_CALL_MESSAGE
    )


def test_a_run_that_finished_or_records_no_call_is_never_failed_by_it(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    finished_id = seeded_run.run_ids.baseline_simulation_run_id
    store.claim_simulation_run(finished_id, modal_call_id="fc-holder", claimed_at=NOW)
    store.complete_simulation_run(
        finished_id,
        modal_call_id="fc-holder",
        output_uri="gs://private/simulations/baseline.parquet",
        output_sha256="a" * 64,
        completed_at=NOW,
    )
    unstarted_id = seeded_run.run_ids.reform_simulation_run_id

    finished = store.fail_simulation_run_held_by(
        finished_id,
        modal_call_id="fc-holder",
        error_message=ENDED_CALL_MESSAGE,
        failed_at=LATER,
    )
    unstarted = store.fail_simulation_run_held_by(
        unstarted_id,
        modal_call_id="fc-holder",
        error_message=ENDED_CALL_MESSAGE,
        failed_at=LATER,
    )

    assert finished == RunWrite(changed=False, status=RunStatus.SUCCEEDED)
    assert unstarted == RunWrite(changed=False, status=RunStatus.PENDING)
    assert seeder.row("simulation_runs", unstarted_id)["error_message"] is None
