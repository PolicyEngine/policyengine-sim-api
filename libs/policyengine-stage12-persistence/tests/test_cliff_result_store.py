"""A simulation run's cliff calculation, written conditionally as the Modal worker role.

The simulation service inserts each cliff result pending, and may record the
Modal call it starts while the row is still pending with none; the seed role
does that here. The worker then claims it, and completes or fails it, each in
one ``UPDATE`` conditional on the row's status and recorded call.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from policyengine_simulation_persistence import CliffImpact
from policyengine_stage12_persistence import (
    CliffResultState,
    CliffResultStore,
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
