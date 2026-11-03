"""The stuck-run sweep's reads and writes, as the Modal worker role.

These run against a disposable Postgres that the Public API's v2 migrations
created (see ``report_run_database``).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from policyengine_stage12_persistence import (
    ReportRunStore,
    RunKind,
    RunningRun,
    RunStatus,
    RunWrite,
)
from report_run_database import NOW, SeededReportRun, worker_database_url
from sqlalchemy import insert

CUTOFF = NOW + timedelta(minutes=30)
ENDED_MESSAGE = "modal_call_ended: the run's Modal call ended without recording it"


@pytest.fixture
def store():
    run_store = ReportRunStore(worker_database_url())
    try:
        yield run_store
    finally:
        run_store.engine.dispose()


def _set_run(seeder, table_name, run_id, **values):
    table = seeder.table(table_name)
    with seeder.engine.begin() as connection:
        connection.execute(table.update().where(table.c.id == run_id).values(**values))


def _running(seeder, seeded_run: SeededReportRun):
    """Mark the report run and baseline run running under calls started at NOW."""
    run_ids = seeded_run.run_ids
    _set_run(
        seeder,
        "report_runs",
        run_ids.report_run_id,
        status="running",
        modal_call_id="fc-coordinator",
        started_at=NOW,
    )
    _set_run(
        seeder,
        "simulation_runs",
        run_ids.baseline_simulation_run_id,
        status="running",
        modal_call_id="fc-baseline",
        started_at=NOW,
    )
    return (
        RunningRun(
            kind=RunKind.REPORT_RUN,
            run_id=run_ids.report_run_id,
            modal_call_id="fc-coordinator",
        ),
        RunningRun(
            kind=RunKind.SIMULATION_RUN,
            run_id=run_ids.baseline_simulation_run_id,
            modal_call_id="fc-baseline",
        ),
    )


def _listed(store, cutoff):
    return (
        *store.list_running_runs(RunKind.REPORT_RUN, started_before=cutoff),
        *store.list_running_runs(RunKind.SIMULATION_RUN, started_before=cutoff),
    )


def _listed_for(store, seeded_run):
    ids = {
        seeded_run.run_ids.report_run_id,
        seeded_run.run_ids.baseline_simulation_run_id,
        seeded_run.run_ids.reform_simulation_run_id,
    }
    return {run for run in _listed(store, CUTOFF) if run.run_id in ids}


def test_the_sweep_lists_runs_running_under_a_call_since_before_the_cutoff(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    report_run, baseline_run = _running(seeder, seeded_run)
    # Only once its report run is no longer running is a simulation run's
    # call the sweep's to check.
    assert _listed_for(store, seeded_run) == {report_run}
    _set_run(
        seeder,
        "report_runs",
        seeded_run.run_ids.report_run_id,
        status="failed",
        completed_at=CUTOFF,
    )
    # The reform run is running, but its call has no ID recorded yet.
    _set_run(
        seeder,
        "simulation_runs",
        seeded_run.run_ids.reform_simulation_run_id,
        status="running",
        started_at=NOW,
    )

    assert _listed_for(store, seeded_run) == {baseline_run}
    assert _listed_for_cutoff(store, seeded_run, NOW) == set()


def _listed_for_cutoff(store, seeded_run, cutoff):
    ids = {
        seeded_run.run_ids.report_run_id,
        seeded_run.run_ids.baseline_simulation_run_id,
    }
    return {run for run in _listed(store, cutoff) if run.run_id in ids}


def test_the_sweep_fails_a_run_once_while_its_ended_call_still_holds_it(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    report_run, baseline_run = _running(seeder, seeded_run)

    first = store.fail_run_of_ended_call(
        report_run, error_message=ENDED_MESSAGE, failed_at=CUTOFF
    )
    again = store.fail_run_of_ended_call(
        report_run, error_message=ENDED_MESSAGE, failed_at=CUTOFF
    )
    simulation = store.fail_run_of_ended_call(
        baseline_run, error_message=ENDED_MESSAGE, failed_at=CUTOFF
    )

    assert first == RunWrite(changed=True, status=RunStatus.FAILED)
    assert again == RunWrite(changed=False, status=RunStatus.FAILED)
    assert simulation == RunWrite(changed=True, status=RunStatus.FAILED)
    row = seeder.row("report_runs", seeded_run.run_ids.report_run_id)
    assert row["error_message"] == ENDED_MESSAGE
    assert row["completed_at"] == CUTOFF


def test_the_sweep_leaves_a_run_that_moved_to_another_call_or_finished(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    report_run, baseline_run = _running(seeder, seeded_run)
    # After the sweep listed them, Modal restarted the baseline's worker
    # under a new call, and the report run succeeded.
    _set_run(
        seeder,
        "simulation_runs",
        seeded_run.run_ids.baseline_simulation_run_id,
        modal_call_id="fc-baseline-restarted",
    )
    _set_run(
        seeder,
        "report_runs",
        seeded_run.run_ids.report_run_id,
        status="succeeded",
        completed_at=CUTOFF,
    )

    moved = store.fail_run_of_ended_call(
        baseline_run, error_message=ENDED_MESSAGE, failed_at=CUTOFF
    )
    finished = store.fail_run_of_ended_call(
        report_run, error_message=ENDED_MESSAGE, failed_at=CUTOFF
    )

    assert moved == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert finished == RunWrite(changed=False, status=RunStatus.SUCCEEDED)


def test_two_sweeps_at_once_fail_each_run_exactly_once(
    seeder, seeded_run: SeededReportRun
) -> None:
    runs = _running(seeder, seeded_run)
    stores = [ReportRunStore(worker_database_url()) for _ in range(2)]
    start_together = Barrier(len(stores))

    def sweep(run_store):
        start_together.wait()
        return [
            run_store.fail_run_of_ended_call(
                run, error_message=ENDED_MESSAGE, failed_at=CUTOFF
            ).changed
            for run in runs
        ]

    try:
        with ThreadPoolExecutor(max_workers=len(stores)) as pool:
            changes = list(pool.map(sweep, stores))
    finally:
        for run_store in stores:
            run_store.engine.dispose()

    assert [sum(column) for column in zip(*changes, strict=True)] == [1, 1]
    assert seeder.row("report_runs", seeded_run.run_ids.report_run_id)["status"] == (
        "failed"
    )


def _second_report_run(seeder, seeded_run: SeededReportRun, status):
    """Insert another report run of the same report, naming the same two runs."""
    first = seeder.row("report_runs", seeded_run.run_ids.report_run_id)
    second_id = uuid4()
    with seeder.engine.begin() as connection:
        connection.execute(
            insert(seeder.table("report_runs")).values(
                id=second_id,
                report_id=first["report_id"],
                baseline_simulation_run_id=first["baseline_simulation_run_id"],
                reform_simulation_run_id=first["reform_simulation_run_id"],
                status=status,
                trigger="manual",
                country_package_version=first["country_package_version"],
                policyengine_version=first["policyengine_version"],
                **({"completed_at": CUTOFF} if status == "succeeded" else {}),
            )
        )
    return second_id


def _baseline_listed(store, seeded_run):
    baseline_id = seeded_run.run_ids.baseline_simulation_run_id
    return any(
        run.run_id == baseline_id
        for run in store.list_running_runs(
            RunKind.SIMULATION_RUN, started_before=CUTOFF
        )
    )


def test_a_simulation_run_is_listed_unless_a_report_run_naming_it_is_running(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    _running(seeder, seeded_run)
    # The first report run is running, so its coordinator judges the run.
    assert not _baseline_listed(store, seeded_run)

    _set_run(seeder, "report_runs", seeded_run.run_ids.report_run_id, status="pending")
    assert _baseline_listed(store, seeded_run)

    second = _second_report_run(seeder, seeded_run, "running")
    assert not _baseline_listed(store, seeded_run)

    _set_run(seeder, "report_runs", second, status="succeeded", completed_at=CUTOFF)
    assert _baseline_listed(store, seeded_run)


def test_runs_past_the_maximum_age_are_listed_and_failed_without_a_call(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    report_run_id = seeded_run.run_ids.report_run_id
    _set_run(seeder, "report_runs", report_run_id, status="running", started_at=NOW)

    with_calls = store.list_running_runs(RunKind.REPORT_RUN, started_before=CUTOFF)
    every_run = store.list_running_runs(
        RunKind.REPORT_RUN, started_before=CUTOFF, include_runs_without_call=True
    )

    assert report_run_id not in {run.run_id for run in with_calls}
    (unrecorded,) = [run for run in every_run if run.run_id == report_run_id]
    assert unrecorded.modal_call_id is None
    failed = store.fail_run_of_ended_call(
        unrecorded, error_message="run_exceeded_maximum_age: too old", failed_at=CUTOFF
    )
    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
