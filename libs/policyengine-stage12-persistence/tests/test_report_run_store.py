"""The Modal worker role's conditional writes to report runs and simulation runs.

These run against a disposable Postgres that the Public API's v2 migrations
created, connected as the Modal worker role (see ``report_run_database``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID, uuid4

import pytest
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_stage12_persistence import (
    ReportRunStore,
    RunStatus,
    RunWrite,
)
from report_run_database import NOW, SeededReportRun, worker_database_url
from sqlalchemy import column, func, insert, select, table, update
from sqlalchemy.exc import ProgrammingError

if TYPE_CHECKING:
    from collections.abc import Callable

LATER = NOW + timedelta(minutes=5)


@pytest.fixture
def store():
    run_store = ReportRunStore(worker_database_url())
    try:
        yield run_store
    finally:
        run_store.engine.dispose()


def test_the_store_connects_as_the_modal_worker_role(store) -> None:
    with store.engine.connect() as connection:
        role = connection.execute(select(func.current_user())).scalar_one()

    assert role == "policyengine_v2_modal_worker"


def test_the_modal_worker_role_cannot_create_a_report_run(store, seeded_run) -> None:
    from policyengine_stage12_persistence.report_run_tables import report_runs

    with (
        pytest.raises(ProgrammingError, match="permission denied"),
        store.engine.begin() as connection,
    ):
        connection.execute(
            insert(report_runs).values(id=uuid4(), status="pending", updated_at=NOW)
        )


class ForbiddenUpdate(NamedTuple):
    """An update of a run column the Modal worker role may not update."""

    table_name: str
    row_id_of: Callable[[ReportRunIds], UUID]
    column_name: str
    value_of: Callable[[ReportRunIds], object]


FORBIDDEN_UPDATES = {
    # What a run is: its simulation runs, its year.
    "report-run-baseline-run": ForbiddenUpdate(
        "report_runs",
        lambda run_ids: run_ids.report_run_id,
        "baseline_simulation_run_id",
        lambda run_ids: run_ids.reform_simulation_run_id,
    ),
    "simulation-run-year": ForbiddenUpdate(
        "simulation_runs",
        lambda run_ids: run_ids.baseline_simulation_run_id,
        "year",
        lambda _run_ids: 2027,
    ),
    # A report run's markdown is not the worker's to write.
    "report-run-markdown": ForbiddenUpdate(
        "report_runs",
        lambda run_ids: run_ids.report_run_id,
        "markdown",
        lambda _run_ids: "# Edited",
    ),
}


@pytest.mark.parametrize(
    "forbidden", list(FORBIDDEN_UPDATES.values()), ids=list(FORBIDDEN_UPDATES)
)
def test_the_modal_worker_role_may_update_only_a_runs_state_and_results(
    store, seeded_run: SeededReportRun, forbidden: ForbiddenUpdate
) -> None:
    run_ids = seeded_run.run_ids
    table_name, column_name = forbidden.table_name, forbidden.column_name
    row_id, value = forbidden.row_id_of(run_ids), forbidden.value_of(run_ids)
    run_table = table(
        table_name,
        column("id"),
        column("updated_at"),
        column(column_name),
        schema="public",
    )
    # The same update of a column the role may update succeeds, so the
    # refusal below is of the column itself, not of the row it selects.
    with store.engine.begin() as connection:
        allowed = connection.execute(
            update(run_table).where(run_table.c.id == row_id).values(updated_at=LATER)
        )
    assert allowed.rowcount == 1

    with (
        pytest.raises(ProgrammingError, match="permission denied"),
        store.engine.begin() as connection,
    ):
        connection.execute(
            update(run_table)
            .where(run_table.c.id == row_id)
            .values({column_name: value})
        )


def test_a_report_run_is_claimed_once_and_resumed_by_the_same_call(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    report_run_id = seeded_run.run_ids.report_run_id

    first = store.claim_report_run(
        report_run_id, modal_call_id="fc-coordinator", claimed_at=NOW
    )
    other_call = store.claim_report_run(
        report_run_id, modal_call_id="fc-other", claimed_at=LATER
    )
    restarted = store.claim_report_run(
        report_run_id, modal_call_id="fc-coordinator", claimed_at=LATER
    )

    assert first == RunWrite(changed=True, status=RunStatus.RUNNING)
    assert other_call == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert restarted == RunWrite(changed=True, status=RunStatus.RUNNING)
    row = seeder.row("report_runs", report_run_id)
    assert row["modal_call_id"] == "fc-coordinator"
    assert row["started_at"] == NOW
    assert row["updated_at"] == LATER


def test_a_pending_report_run_is_claimed_only_by_the_call_recorded_on_it(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    # The simulation service records the call it started while the run is
    # still pending; no other call may take the run.
    report_run_id = seeded_run.run_ids.report_run_id
    report_runs = seeder.table("report_runs")
    with seeder.engine.begin() as connection:
        connection.execute(
            report_runs.update()
            .where(report_runs.c.id == report_run_id)
            .values(modal_call_id="fc-coordinator")
        )

    other_call = store.claim_report_run(
        report_run_id, modal_call_id="fc-other", claimed_at=NOW
    )
    recorded_call = store.claim_report_run(
        report_run_id, modal_call_id="fc-coordinator", claimed_at=LATER
    )

    assert other_call == RunWrite(changed=False, status=RunStatus.PENDING)
    assert recorded_call == RunWrite(changed=True, status=RunStatus.RUNNING)
    row = seeder.row("report_runs", report_run_id)
    assert row["modal_call_id"] == "fc-coordinator"
    assert row["started_at"] == LATER


def test_a_running_report_run_with_no_call_recorded_is_claimed(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    # Nothing now marks a report run running without recording its call, but
    # a run left that way is still claimed, keeping its first start.
    report_run_id = seeded_run.run_ids.report_run_id
    report_runs = seeder.table("report_runs")
    with seeder.engine.begin() as connection:
        connection.execute(
            report_runs.update()
            .where(report_runs.c.id == report_run_id)
            .values(status="running", started_at=NOW)
        )

    claimed = store.claim_report_run(
        report_run_id, modal_call_id="fc-coordinator", claimed_at=LATER
    )

    assert claimed == RunWrite(changed=True, status=RunStatus.RUNNING)
    assert seeder.row("report_runs", report_run_id)["started_at"] == NOW


def test_a_report_run_completes_only_while_its_call_holds_it(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    report_run_id = seeded_run.run_ids.report_run_id
    store.claim_report_run(
        report_run_id, modal_call_id="fc-coordinator", claimed_at=NOW
    )

    by_other_call = store.complete_report_run(
        seeded_run.run_ids, modal_call_id="fc-other", completed_at=LATER
    )
    completed = store.complete_report_run(
        seeded_run.run_ids, modal_call_id="fc-coordinator", completed_at=LATER
    )
    repeated = store.complete_report_run(
        seeded_run.run_ids, modal_call_id="fc-coordinator", completed_at=LATER
    )
    reclaimed = store.claim_report_run(
        report_run_id, modal_call_id="fc-coordinator", claimed_at=LATER
    )

    assert by_other_call == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert completed == RunWrite(changed=True, status=RunStatus.SUCCEEDED)
    assert repeated == RunWrite(changed=False, status=RunStatus.SUCCEEDED)
    assert reclaimed == RunWrite(changed=False, status=RunStatus.SUCCEEDED)
    row = seeder.row("report_runs", report_run_id)
    assert row["status"] == "succeeded"
    assert row["completed_at"] == LATER
    assert row["error_message"] is None


def test_a_report_run_fails_once_and_then_never_completes(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    report_run_id = seeded_run.run_ids.report_run_id
    store.claim_report_run(
        report_run_id, modal_call_id="fc-coordinator", claimed_at=NOW
    )

    failed = store.fail_report_run(
        report_run_id,
        modal_call_id="fc-coordinator",
        error_message="simulation_execution_failed: the reform failed",
        failed_at=LATER,
    )
    again = store.fail_report_run(
        report_run_id,
        modal_call_id="fc-coordinator",
        error_message="another failure",
        failed_at=LATER,
    )
    completed = store.complete_report_run(
        seeded_run.run_ids, modal_call_id="fc-coordinator", completed_at=LATER
    )

    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    assert again == RunWrite(changed=False, status=RunStatus.FAILED)
    assert completed == RunWrite(changed=False, status=RunStatus.FAILED)
    row = seeder.row("report_runs", report_run_id)
    assert row["error_message"] == "simulation_execution_failed: the reform failed"
    assert row["completed_at"] == LATER


def test_a_simulation_run_moves_to_the_latest_call_until_it_finishes(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    run_id = seeded_run.run_ids.baseline_simulation_run_id

    first = store.claim_simulation_run(
        run_id, modal_call_id="fc-worker-1", claimed_at=NOW
    )
    # The coordinator started this simulation again after a restart.
    second = store.claim_simulation_run(
        run_id, modal_call_id="fc-worker-2", claimed_at=LATER
    )
    completed = store.complete_simulation_run(
        run_id,
        output_uri="gs://private/simulations/baseline.parquet",
        output_sha256="a" * 64,
        completed_at=LATER,
    )
    repeated = store.complete_simulation_run(
        run_id,
        output_uri="gs://private/simulations/baseline-other.parquet",
        output_sha256="b" * 64,
        completed_at=LATER,
    )
    failed_after = store.fail_simulation_run(
        run_id, error_message="cancelled", failed_at=LATER
    )
    claimed_after = store.claim_simulation_run(
        run_id, modal_call_id="fc-worker-3", claimed_at=LATER
    )

    assert first == RunWrite(changed=True, status=RunStatus.RUNNING)
    assert second == RunWrite(changed=True, status=RunStatus.RUNNING)
    assert completed == RunWrite(changed=True, status=RunStatus.SUCCEEDED)
    assert repeated == RunWrite(changed=False, status=RunStatus.SUCCEEDED)
    assert failed_after == RunWrite(changed=False, status=RunStatus.SUCCEEDED)
    assert claimed_after == RunWrite(changed=False, status=RunStatus.SUCCEEDED)
    row = seeder.row("simulation_runs", run_id)
    assert row["modal_call_id"] == "fc-worker-2"
    assert row["started_at"] == NOW
    assert row["output_uri"] == "gs://private/simulations/baseline.parquet"
    assert row["output_sha256"] == "a" * 64


def test_a_pending_or_running_simulation_run_can_fail_but_not_resume(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    run_id = seeded_run.run_ids.reform_simulation_run_id

    failed = store.fail_simulation_run(
        run_id, error_message="simulation_cancelled: stopped", failed_at=NOW
    )
    claimed = store.claim_simulation_run(
        run_id, modal_call_id="fc-worker", claimed_at=LATER
    )
    completed = store.complete_simulation_run(
        run_id,
        output_uri="gs://private/x.parquet",
        output_sha256="c" * 64,
        completed_at=LATER,
    )

    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    assert claimed == RunWrite(changed=False, status=RunStatus.FAILED)
    assert completed == RunWrite(changed=False, status=RunStatus.FAILED)
    row = seeder.row("simulation_runs", run_id)
    assert row["error_message"] == "simulation_cancelled: stopped"
    assert row["output_uri"] is None


def test_a_report_run_must_name_the_simulation_runs_its_call_names(
    store, seeded_run: SeededReportRun
) -> None:
    run_ids = seeded_run.run_ids
    store.require_simulation_runs(run_ids)

    swapped = ReportRunIds(
        report_run_id=run_ids.report_run_id,
        baseline_simulation_run_id=run_ids.reform_simulation_run_id,
        reform_simulation_run_id=run_ids.baseline_simulation_run_id,
    )
    another_reform = ReportRunIds(
        report_run_id=run_ids.report_run_id,
        baseline_simulation_run_id=run_ids.baseline_simulation_run_id,
        reform_simulation_run_id=uuid4(),
    )
    for named_runs in (swapped, another_reform):
        with pytest.raises(ValueError, match="names other simulation runs"):
            store.require_simulation_runs(named_runs)
    missing = ReportRunIds(
        report_run_id=uuid4(),
        baseline_simulation_run_id=run_ids.baseline_simulation_run_id,
        reform_simulation_run_id=run_ids.reform_simulation_run_id,
    )
    with pytest.raises(LookupError, match="report run"):
        store.require_simulation_runs(missing)


def test_a_missing_run_raises_lookup_error(store, seeded_run) -> None:
    with pytest.raises(LookupError, match="report run"):
        store.claim_report_run(uuid4(), modal_call_id="fc-coordinator", claimed_at=NOW)
    with pytest.raises(LookupError, match="simulation run"):
        store.fail_simulation_run(uuid4(), error_message="failed", failed_at=NOW)
