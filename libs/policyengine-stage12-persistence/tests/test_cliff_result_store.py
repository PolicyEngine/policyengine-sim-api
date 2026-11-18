"""A simulation run's cliff calculation, written conditionally as the Modal worker role.

The simulation service inserts each cliff result pending, and may record the
Modal call it starts while the row is still pending with none; the seed role
does that here. The worker then claims it, and completes or fails it, each in
one ``UPDATE`` conditional on the row's status and recorded call.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from policyengine_simulation_persistence import CliffImpact
from policyengine_stage12_persistence import (
    ActiveRun,
    CliffResultState,
    CliffResultStore,
    RunKind,
    RunStatus,
    RunWrite,
)
from report_run_database import NOW, SeededReportRun, worker_database_url
from sqlalchemy import insert, select, update
from sqlalchemy.exc import ProgrammingError

WORKER_CALL_ID = "fc-cliff-worker"
OTHER_CALL_ID = "fc-other-cliff-worker"
LATER = NOW + timedelta(minutes=2)
CLIFF_GAP = 1_234.5
CLIFF_SHARE = 0.0375
# The sweep's cutoff: a cliff result active since before it is listed.
CUTOFF = NOW + timedelta(hours=1)
ENDED_MESSAGE = "modal_call_ended: the call ended without an outcome"


@pytest.fixture
def store():
    return CliffResultStore(worker_database_url())


def _insert_cliff_result(seeder, seeded_run: SeededReportRun, **values):
    """Insert a cliff result of the seeded baseline run, as the service does."""
    cliff_result_id = uuid4()
    with seeder.engine.begin() as connection:
        connection.execute(
            insert(CliffImpact.__table__).values(
                id=cliff_result_id,
                simulation_run_id=seeded_run.run_ids.baseline_simulation_run_id,
                status=values.pop("status", "pending"),
                **values,
            )
        )
    return cliff_result_id


def _cliff_row(seeder, cliff_result_id):
    with seeder.engine.connect() as connection:
        row = connection.execute(
            select(CliffImpact.__table__).where(CliffImpact.id == cliff_result_id)
        ).one()
    return dict(row._mapping)


def _claim(store, cliff_result_id, modal_call_id=WORKER_CALL_ID):
    return store.claim_cliff_result(
        cliff_result_id, modal_call_id=modal_call_id, claimed_at=NOW
    )


def _complete(store, cliff_result_id, modal_call_id=WORKER_CALL_ID):
    return store.complete_cliff_result(
        cliff_result_id,
        modal_call_id=modal_call_id,
        cliff_gap=CLIFF_GAP,
        cliff_share=CLIFF_SHARE,
        completed_at=LATER,
    )


def test_a_cliff_result_is_claimed_once_and_resumed_by_the_same_call(
    store, seeder, seeded_run
) -> None:
    cliff_result_id = _insert_cliff_result(seeder, seeded_run)

    assert _claim(store, cliff_result_id) == RunWrite(
        changed=True, status=RunStatus.RUNNING
    )
    # Modal restarts a pre-empted call under the same call ID.
    assert _claim(store, cliff_result_id) == RunWrite(
        changed=True, status=RunStatus.RUNNING
    )
    # Another call is the second start of the same calculation.
    assert _claim(store, cliff_result_id, OTHER_CALL_ID) == RunWrite(
        changed=False, status=RunStatus.RUNNING
    )

    row = _cliff_row(seeder, cliff_result_id)
    assert (row["status"], row["modal_call_id"]) == ("running", WORKER_CALL_ID)
    assert row["started_at"] == NOW


def test_concurrent_starts_leave_the_cliff_result_with_one_call(
    worker_sessions, statements, seeder, seeded_run
) -> None:
    """Two starts calculate once, however close together they claim the row."""
    assert statements.sent == []
    cliff_result_id = _insert_cliff_result(seeder, seeded_run)
    claimants = 6
    start_together = Barrier(claimants)

    def claim(claimant: int) -> tuple[str, RunWrite]:
        claiming_store = CliffResultStore(session_factory=worker_sessions)
        call = f"fc-claimant-{claimant}"
        start_together.wait(timeout=10)
        return call, _claim(claiming_store, cliff_result_id, call)

    with ThreadPoolExecutor(max_workers=claimants) as executor:
        outcomes = list(executor.map(claim, range(claimants)))

    winners = [call for call, write in outcomes if write.changed]
    assert len(winners) == 1
    assert _cliff_row(seeder, cliff_result_id)["modal_call_id"] == winners[0]
    assert all(write.status is RunStatus.RUNNING for _call, write in outcomes)


def test_a_pending_cliff_result_is_claimed_only_by_the_call_recorded_on_it(
    store, seeder, seeded_run
) -> None:
    cliff_result_id = _insert_cliff_result(
        seeder, seeded_run, modal_call_id=WORKER_CALL_ID
    )

    assert _claim(store, cliff_result_id, OTHER_CALL_ID) == RunWrite(
        changed=False, status=RunStatus.PENDING
    )
    assert _claim(store, cliff_result_id) == RunWrite(
        changed=True, status=RunStatus.RUNNING
    )


def test_a_cliff_result_completes_with_its_values_while_its_call_holds_it(
    store, seeder, seeded_run
) -> None:
    cliff_result_id = _insert_cliff_result(seeder, seeded_run)

    # Not claimed yet: no call holds it.
    assert _complete(store, cliff_result_id) == RunWrite(
        changed=False, status=RunStatus.PENDING
    )
    _claim(store, cliff_result_id)
    assert _complete(store, cliff_result_id, OTHER_CALL_ID) == RunWrite(
        changed=False, status=RunStatus.RUNNING
    )
    assert _complete(store, cliff_result_id) == RunWrite(
        changed=True, status=RunStatus.SUCCEEDED
    )
    # A completed calculation is written once.
    assert _complete(store, cliff_result_id) == RunWrite(
        changed=False, status=RunStatus.SUCCEEDED
    )
    assert _claim(store, cliff_result_id) == RunWrite(
        changed=False, status=RunStatus.SUCCEEDED
    )

    row = _cliff_row(seeder, cliff_result_id)
    assert (row["status"], row["cliff_gap"], row["cliff_share"]) == (
        "succeeded",
        CLIFF_GAP,
        CLIFF_SHARE,
    )
    assert (row["completed_at"], row["error_message"]) == (LATER, None)


def test_a_failed_cliff_calculation_records_its_failure_and_never_completes(
    store, seeder, seeded_run
) -> None:
    cliff_result_id = _insert_cliff_result(seeder, seeded_run)
    _claim(store, cliff_result_id)

    failure = {
        "error_message": "cliff_calculation_failed: the calculation failed",
        "failed_at": LATER,
    }
    assert store.fail_cliff_result(
        cliff_result_id, modal_call_id=OTHER_CALL_ID, **failure
    ) == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert store.fail_cliff_result(
        cliff_result_id, modal_call_id=WORKER_CALL_ID, **failure
    ) == RunWrite(changed=True, status=RunStatus.FAILED)
    assert _complete(store, cliff_result_id) == RunWrite(
        changed=False, status=RunStatus.FAILED
    )
    assert _claim(store, cliff_result_id) == RunWrite(
        changed=False, status=RunStatus.FAILED
    )

    row = _cliff_row(seeder, cliff_result_id)
    assert row["status"] == "failed"
    assert row["error_message"] == failure["error_message"]
    assert (row["completed_at"], row["cliff_gap"], row["cliff_share"]) == (
        LATER,
        None,
        None,
    )


def test_a_cliff_results_state_names_its_run_call_values_and_failure(
    store, seeder, seeded_run
) -> None:
    cliff_result_id = _insert_cliff_result(seeder, seeded_run)
    _claim(store, cliff_result_id)
    _complete(store, cliff_result_id)

    assert store.get_cliff_result(cliff_result_id) == CliffResultState(
        simulation_run_id=seeded_run.run_ids.baseline_simulation_run_id,
        status=RunStatus.SUCCEEDED,
        modal_call_id=WORKER_CALL_ID,
        cliff_gap=CLIFF_GAP,
        cliff_share=CLIFF_SHARE,
        error_message=None,
    )
    # The cliff store reads the run it was calculated for, as the worker checks.
    run = store.get_simulation_run(seeded_run.run_ids.baseline_simulation_run_id)
    assert (run.year, run.status) == (2026, RunStatus.PENDING)


def test_a_missing_cliff_result_raises_lookup_error(store, seeded_run) -> None:
    missing = uuid4()

    with pytest.raises(LookupError, match=f"cliff result {missing} does not exist"):
        store.get_cliff_result(missing)
    with pytest.raises(LookupError, match=f"cliff result {missing} does not exist"):
        _claim(store, missing)


def test_the_modal_worker_role_cannot_insert_or_retarget_a_cliff_result(
    store, seeder, seeded_run
) -> None:
    cliff_result_id = _insert_cliff_result(seeder, seeded_run)

    with (
        pytest.raises(ProgrammingError, match="permission denied"),
        store.sessions.transaction("create_cliff_result") as session,
    ):
        session.execute(
            insert(CliffImpact).values(
                id=uuid4(),
                simulation_run_id=seeded_run.run_ids.reform_simulation_run_id,
                status="pending",
            )
        )
    with (
        pytest.raises(ProgrammingError, match="permission denied"),
        store.sessions.transaction("retarget_cliff_result") as session,
    ):
        session.execute(
            update(CliffImpact)
            .where(CliffImpact.id == cliff_result_id)
            .values(simulation_run_id=seeded_run.run_ids.reform_simulation_run_id),
            execution_options={"synchronize_session": False},
        )


def _listed_cliff_results(store, cliff_result_id, cutoff=CUTOFF) -> set[ActiveRun]:
    """Return the sweep's listing of cliff results, narrowed to ``cliff_result_id``."""
    return {
        listed
        for listed in store.list_active_runs(
            RunKind.CLIFF_RESULT, active_since_before=cutoff
        )
        if listed.run_id == cliff_result_id
    }


def test_the_sweep_lists_a_cliff_result_from_its_claim_or_its_creation(
    store, seeder, seeded_run
) -> None:
    cliff_result_id = _insert_cliff_result(seeder, seeded_run, created_at=NOW)

    assert _listed_cliff_results(store, cliff_result_id) == {
        ActiveRun(
            kind=RunKind.CLIFF_RESULT,
            run_id=cliff_result_id,
            status=RunStatus.PENDING,
            modal_call_id=None,
        )
    }
    assert _listed_cliff_results(store, cliff_result_id, NOW) == set()

    # Once claimed, it counts from its claim, after the cutoff here.
    store.claim_cliff_result(
        cliff_result_id,
        modal_call_id=WORKER_CALL_ID,
        claimed_at=CUTOFF + timedelta(hours=1),
    )
    assert _listed_cliff_results(store, cliff_result_id) == set()
    assert _listed_cliff_results(
        store, cliff_result_id, CUTOFF + timedelta(hours=2)
    ) == {
        ActiveRun(
            kind=RunKind.CLIFF_RESULT,
            run_id=cliff_result_id,
            status=RunStatus.RUNNING,
            modal_call_id=WORKER_CALL_ID,
        )
    }


def test_the_sweep_fails_a_cliff_result_only_while_it_is_as_listed(
    store, seeder, seeded_run
) -> None:
    cliff_result_id = _insert_cliff_result(seeder, seeded_run, created_at=NOW)
    (listed_without_call,) = _listed_cliff_results(store, cliff_result_id)
    # After the listing, the simulation service recorded the call it started.
    with seeder.engine.begin() as connection:
        connection.execute(
            update(CliffImpact.__table__)
            .where(CliffImpact.id == cliff_result_id)
            .values(modal_call_id=WORKER_CALL_ID)
        )
    late_call = store.fail_stuck_run(
        listed_without_call, error_message=ENDED_MESSAGE, failed_at=CUTOFF
    )
    (listed_with_call,) = _listed_cliff_results(store, cliff_result_id)

    first = store.fail_stuck_run(
        listed_with_call, error_message=ENDED_MESSAGE, failed_at=CUTOFF
    )
    again = store.fail_stuck_run(
        listed_with_call, error_message=ENDED_MESSAGE, failed_at=CUTOFF
    )

    assert late_call == RunWrite(changed=False, status=RunStatus.PENDING)
    assert first == RunWrite(changed=True, status=RunStatus.FAILED)
    assert again == RunWrite(changed=False, status=RunStatus.FAILED)
    row = _cliff_row(seeder, cliff_result_id)
    assert (row["status"], row["error_message"]) == ("failed", ENDED_MESSAGE)
    assert (row["completed_at"], row["updated_at"]) == (CUTOFF, CUTOFF)
