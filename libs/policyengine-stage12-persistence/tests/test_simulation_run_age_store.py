"""The stuck-run sweep's age limit, for a simulation run report runs still name.

Identical requests share simulation runs, so new report runs may keep naming,
and waiting for, a run whose call Modal never ends. The sweep's age limit,
longer than any Modal call of a run can take, lists and fails such a run
whatever report runs name it; the sweep's other passes still leave a run an
unfinished report run names to that report run's coordinator.

These run against a disposable Postgres that the Public API's v2 migrations
created, connected as the Modal worker role (see ``report_run_database``).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from policyengine_stage12_persistence import (
    ReportRunStore,
    RunKind,
    RunStatus,
    RunWrite,
)
from report_run_database import NOW, SeededReportRun, worker_database_url

MAXIMUM_AGE = timedelta(hours=24)
LONG_AGO = NOW - MAXIMUM_AGE - timedelta(hours=1)
EXCEEDED_AGE_MESSAGE = "run_exceeded_maximum_age: too old"


@pytest.fixture
def store():
    run_store = ReportRunStore(worker_database_url())
    try:
        yield run_store
    finally:
        run_store.engine.dispose()


def _listed(store, run_id, **options):
    return [
        run
        for run in store.list_active_runs(
            RunKind.SIMULATION_RUN, active_since_before=NOW - MAXIMUM_AGE, **options
        )
        if run.run_id == run_id
    ]


def test_an_old_run_a_running_report_run_names_is_listed_and_failed_for_its_age(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    run_ids = seeded_run.run_ids
    store.claim_report_run(
        run_ids.report_run_id, modal_call_id="fc-waiting-coordinator", claimed_at=NOW
    )
    run_id = run_ids.baseline_simulation_run_id
    store.claim_simulation_run(
        run_id, modal_call_id="fc-never-ending", claimed_at=LONG_AGO
    )

    assert _listed(store, run_id) == []
    (listed,) = _listed(store, run_id, regardless_of_report_runs=True)
    left = store.fail_stuck_run(
        listed, error_message=EXCEEDED_AGE_MESSAGE, failed_at=NOW
    )
    failed = store.fail_stuck_run(
        listed,
        error_message=EXCEEDED_AGE_MESSAGE,
        failed_at=NOW,
        regardless_of_report_runs=True,
    )

    assert left == RunWrite(changed=False, status=RunStatus.RUNNING)
    assert failed == RunWrite(changed=True, status=RunStatus.FAILED)
    assert seeder.row("simulation_runs", run_id)["error_message"] == (
        EXCEEDED_AGE_MESSAGE
    )
