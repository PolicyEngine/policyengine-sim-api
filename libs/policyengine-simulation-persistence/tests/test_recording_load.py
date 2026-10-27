"""A load check of the recording passes' conditional writes on PostgreSQL.

Several simulated Simulation Entrypoint instances, each on its own engine,
repeatedly fail stalled dispatches, claim due calls and record an outcome for
each, as recording passes do, until every run has finished. A third of
the calls are household pairs (two runs under one call). Every call must be
recorded once, its results written once and all of its runs finished once,
and every stalled dispatch failed once, whatever the interleaving. The
elapsed time and the number of passes are printed, as a measurement; nothing
depends on them.
"""

from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Lock

import pytest
from policyengine_simulation_persistence import (
    RunError,
    RunErrorCode,
    RunStatus,
    RunTransitions,
    create_simulation_engine,
)
from policyengine_simulation_persistence.runs import simulation_runs
from run_support import household_run
from simulation_support import household_simulation, simulation_store
from sqlalchemy import func, select
from transition_support import ManualClock, clocked_stores

pytestmark = pytest.mark.postgres

CALLS = 480
# Every third call is a household report's pair: two runs under one call.
PAIR_EVERY = 3
# Runs claimed by an instance that stopped before recording their call.
STALLED_DISPATCHES = 30
INSTANCES = 6
CLAIM_LIMIT = 20
LEASE = timedelta(minutes=1)
ABANDONED_AFTER = timedelta(minutes=10)
MAXIMUM_PASSES = 400


def _household_run(stores, simulations, rows, legacy_id: int):
    simulation = simulations.create(
        household_simulation(rows, legacy_simulation_id=legacy_id)
    )
    return stores.runs.start_simulation_run(household_run(simulation, rows)).run


def _start_running_calls(database, rows, clock) -> dict[str, int]:
    """Create running calls, a third of them household pairs, and stalled runs.

    Return each call's number of runs.
    """
    stores = clocked_stores(database, clock)
    simulations = simulation_store(database)
    legacy_ids = iter(range(1, 10_000))
    runs_per_call = {}
    for index in range(CALLS):
        count = 2 if index % PAIR_EVERY == 0 else 1
        run_ids = [
            _household_run(stores, simulations, rows, next(legacy_ids)).id
            for _ in range(count)
        ]
        call_id = f"fc-load-{index}"
        stores.transitions.claim_simulation_dispatch(run_ids)
        stores.transitions.record_simulation_dispatched(
            run_ids, modal_call_id=call_id, first_check_at=clock()
        )
        runs_per_call[call_id] = count
    for _ in range(STALLED_DISPATCHES):
        run = _household_run(stores, simulations, rows, next(legacy_ids))
        stores.transitions.claim_simulation_dispatch([run.id])
    stores.transitions.engine.dispose()
    return runs_per_call


class _Outcomes:
    """Remember every recorded outcome, from every instance."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.written_calls: list[str] = []
        self.failed_calls: list[str] = []
        self.abandoned_runs: list[object] = []

    def write(self, call_id: str):
        def write_results(_connection, _runs) -> None:
            with self.lock:
                self.written_calls.append(call_id)

        return write_results


def _record(transitions, outcomes, call_id: str, *, succeed: bool) -> None:
    if succeed:
        transitions.complete_simulation_call(call_id, outcomes.write(call_id))
    elif transitions.fail_simulation_call(
        call_id, RunError(RunErrorCode.WORKER_ERROR, "The Modal call raised.")
    ):
        with outcomes.lock:
            outcomes.failed_calls.append(call_id)


def _instance(database, clock, outcomes, barrier, seed: int) -> int:
    """Claim and record due calls until none is left; return the pass count.

    Some outcomes are recorded only in a later pass, after an instance with
    nothing to claim has moved the clock past every lease, so another
    instance may claim the same call and record it first: the late write
    must then change nothing.
    """
    chooser = random.Random(seed)  # noqa: S311  # test interleaving, not security
    transitions = RunTransitions(
        create_simulation_engine(database.runtime_url), clock=clock
    )
    late: list[tuple[str, bool]] = []
    barrier.wait()
    passes = 0
    try:
        while passes < MAXIMUM_PASSES:
            passes += 1
            for call_id, succeed in late:
                _record(transitions, outcomes, call_id, succeed=succeed)
            late.clear()
            abandoned = transitions.fail_abandoned_dispatches(
                claimed_before=clock() - ABANDONED_AFTER
            )
            with outcomes.lock:
                outcomes.abandoned_runs.extend(abandoned.simulation_run_ids)
            calls = transitions.claim_due_simulation_calls(
                limit=CLAIM_LIMIT, lease=LEASE
            )
            if not calls:
                if (
                    not _unfinished(transitions)
                    and len(outcomes.abandoned_runs) == STALLED_DISPATCHES
                ):
                    return passes
                clock.advance(LEASE)
                continue
            for call in calls:
                outcome = chooser.random()
                if outcome < 0.3:
                    transitions.record_simulation_call_running(
                        call.modal_call_id, next_check_at=clock()
                    )
                elif outcome < 0.5:
                    late.append((call.modal_call_id, outcome < 0.45))
                else:
                    _record(
                        transitions, outcomes, call.modal_call_id, succeed=outcome < 0.9
                    )
        return passes
    finally:
        transitions.engine.dispose()


def _unfinished(transitions: RunTransitions) -> int:
    with transitions.engine.connect() as connection:
        return connection.execute(
            select(func.count()).where(
                simulation_runs.c.status == RunStatus.RUNNING.value
            )
        ).scalar_one()


def test_concurrent_instances_record_every_call_once(
    migrated_database, public_api_rows, capsys
) -> None:
    clock = ManualClock()
    runs_per_call = _start_running_calls(migrated_database, public_api_rows, clock)
    outcomes = _Outcomes()
    barrier = Barrier(INSTANCES)
    started = time.monotonic()

    with ThreadPoolExecutor(max_workers=INSTANCES) as pool:
        passes = list(
            pool.map(
                lambda seed: _instance(
                    migrated_database, clock, outcomes, barrier, seed
                ),
                range(INSTANCES),
            )
        )

    elapsed = time.monotonic() - started
    recorded = outcomes.written_calls + outcomes.failed_calls
    assert sorted(recorded) == sorted(runs_per_call)
    assert len(outcomes.abandoned_runs) == len(set(outcomes.abandoned_runs))
    assert len(outcomes.abandoned_runs) == STALLED_DISPATCHES
    stores = clocked_stores(migrated_database, clock)
    with stores.transitions.engine.connect() as connection:
        statuses = dict(
            connection.execute(
                select(simulation_runs.c.status, func.count()).group_by(
                    simulation_runs.c.status
                )
            ).all()
        )
    stores.transitions.engine.dispose()
    assert statuses == {
        RunStatus.SUCCEEDED.value: sum(
            runs_per_call[call_id] for call_id in outcomes.written_calls
        ),
        RunStatus.FAILED.value: STALLED_DISPATCHES
        + sum(runs_per_call[call_id] for call_id in outcomes.failed_calls),
    }
    with capsys.disabled():
        print(  # noqa: T201  # the measurement this check records
            f"\nrecording load check: {len(runs_per_call)} calls "
            f"({sum(runs_per_call.values())} runs), {STALLED_DISPATCHES} stalled "
            f"dispatches, {INSTANCES} instances, {sum(passes)} passes, "
            f"{elapsed:.2f} s"
        )
