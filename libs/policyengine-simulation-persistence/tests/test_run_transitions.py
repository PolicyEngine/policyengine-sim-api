"""Conditional status changes: a second writer never changes anything."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from policyengine_simulation_persistence import (
    NewReportRun,
    ReportKind,
    RunError,
    RunErrorCode,
    RunStatus,
    RunTransitions,
    RunTrigger,
    create_simulation_engine,
)
from report_support import economy_pair, economy_report, household_baseline
from report_support import report_store as definition_report_store
from run_support import economy_run, household_run
from simulation_support import household_simulation, simulation_store
from transition_support import ManualClock, clocked_stores

pytestmark = pytest.mark.postgres

LEASE = timedelta(minutes=1)
FIRST_CHECK = timedelta(seconds=2)
WORKER_ERROR = RunError(RunErrorCode.WORKER_ERROR, "The Modal call raised KeyError.")


def _pending_run(stores, database, rows):
    baseline, _ = economy_pair(database, rows)
    return stores.runs.start_simulation_run(economy_run(baseline, rows)).run


def _running_run(stores, database, rows, call_id="fc-economy"):
    run = _pending_run(stores, database, rows)
    assert stores.transitions.claim_simulation_dispatch([run.id])
    assert stores.transitions.record_simulation_dispatched(
        [run.id], modal_call_id=call_id, first_check_at=stores.clock() + FIRST_CHECK
    )
    return stores.runs.get_simulation_run(run.id)


def _household_pair(stores, database, rows):
    baseline = household_baseline(database, rows)
    reform = simulation_store(database).create(
        household_simulation(rows, policy_id=rows.us_policy_id)
    )
    return (
        stores.runs.start_simulation_run(household_run(baseline, rows)).run,
        stores.runs.start_simulation_run(household_run(reform, rows)).run,
    )


def _no_results(_connection, _records) -> None:
    return None


def test_only_one_claim_moves_a_run_to_dispatching(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    run = _pending_run(stores, migrated_database, public_api_rows)

    assert stores.transitions.claim_simulation_dispatch([run.id]) is True
    assert stores.transitions.claim_simulation_dispatch([run.id]) is False

    claimed = stores.runs.get_simulation_run(run.id)
    assert claimed.status is RunStatus.DISPATCHING
    assert claimed.modal_call_id is None


def test_concurrent_claims_start_one_modal_call(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    run = _pending_run(stores, migrated_database, public_api_rows)
    instances = 8
    barrier = Barrier(instances)

    def claim(_instance: int) -> bool:
        transitions = RunTransitions(
            create_simulation_engine(migrated_database.runtime_url)
        )
        try:
            barrier.wait()
            return transitions.claim_simulation_dispatch([run.id])
        finally:
            transitions.engine.dispose()

    with ThreadPoolExecutor(max_workers=instances) as pool:
        outcomes = list(pool.map(claim, range(instances)))

    assert outcomes.count(True) == 1


def test_a_dispatched_run_records_its_call_and_first_check(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    run = _running_run(stores, migrated_database, public_api_rows)

    assert run.status is RunStatus.RUNNING
    assert run.modal_call_id == "fc-economy"
    assert run.dispatched_at == stores.clock()
    assert run.next_check_at == stores.clock() + FIRST_CHECK
    assert not stores.transitions.record_simulation_dispatched(
        [run.id], modal_call_id="fc-other", first_check_at=stores.clock()
    )


def test_runs_say_whether_they_recorded_a_call(
    migrated_database, public_api_rows
) -> None:
    """A dispatcher whose record raised reads whether it committed all the same."""
    stores = clocked_stores(migrated_database)
    running = _running_run(stores, migrated_database, public_api_rows)
    dispatching = _pending_run(stores, migrated_database, public_api_rows)
    stores.transitions.claim_simulation_dispatch([dispatching.id])
    transitions = stores.transitions

    assert transitions.simulation_runs_hold_call([running.id], "fc-economy")
    assert not transitions.simulation_runs_hold_call([running.id], "fc-other")
    assert not transitions.simulation_runs_hold_call([dispatching.id], "fc-economy")
    assert not transitions.simulation_runs_hold_call(
        [running.id, dispatching.id], "fc-economy"
    )
    assert not transitions.simulation_runs_hold_call([], "fc-economy")


def test_a_refused_dispatch_fails_the_run_and_frees_the_setup(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    run = _pending_run(stores, migrated_database, public_api_rows)
    stores.transitions.claim_simulation_dispatch([run.id])

    assert stores.transitions.record_simulation_dispatch_failure(
        [run.id], RunError(RunErrorCode.DISPATCH_FAILED, "Modal refused the call.")
    )

    failed = stores.runs.get_simulation_run(run.id)
    assert failed.status is RunStatus.FAILED
    assert failed.error_code == "dispatch_failed"
    assert failed.completed_at == stores.clock()
    simulation = simulation_store(migrated_database).get(run.simulation_id)
    retry = stores.runs.start_simulation_run(economy_run(simulation, public_api_rows))
    assert retry.created is True
    assert retry.run.attempt == 2


def test_a_household_pair_shares_one_call_and_changes_together(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    baseline_run, reform_run = _household_pair(
        stores, migrated_database, public_api_rows
    )
    pair = [baseline_run.id, reform_run.id]
    written = []

    assert stores.transitions.claim_simulation_dispatch(pair)
    assert stores.transitions.record_simulation_dispatched(
        pair, modal_call_id="fc-household", first_check_at=stores.clock()
    )
    assert stores.transitions.complete_simulation_call(
        "fc-household", lambda _connection, runs: written.extend(runs)
    )

    assert {run.id for run in written} == set(pair)
    for run_id in pair:
        run = stores.runs.get_simulation_run(run_id)
        assert run.status is RunStatus.SUCCEEDED
        assert run.modal_call_id == "fc-household"
        assert run.next_check_at is None


def test_a_group_claim_changes_nothing_unless_every_run_is_pending(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    baseline_run, reform_run = _household_pair(
        stores, migrated_database, public_api_rows
    )
    assert stores.transitions.claim_simulation_dispatch([baseline_run.id])

    assert not stores.transitions.claim_simulation_dispatch(
        [baseline_run.id, reform_run.id]
    )

    assert stores.runs.get_simulation_run(reform_run.id).status is RunStatus.PENDING
    with pytest.raises(ValueError, match="at least one run"):
        stores.transitions.claim_simulation_dispatch([])


def test_due_calls_are_claimed_for_one_lease_at_a_time(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    run = _running_run(stores, migrated_database, public_api_rows)
    transitions = stores.transitions

    assert transitions.claim_due_simulation_calls(limit=10, lease=LEASE) == []
    stores.clock.advance(FIRST_CHECK)
    claimed = transitions.claim_due_simulation_calls(limit=10, lease=LEASE)
    assert [call.modal_call_id for call in claimed] == ["fc-economy"]
    assert claimed[0].runs[0].id == run.id
    assert claimed[0].runs[0].check_count == 1
    assert claimed[0].runs[0].next_check_at == stores.clock() + LEASE
    assert transitions.claim_due_simulation_calls(limit=10, lease=LEASE) == []

    # The lease ends when the claiming instance stops before recording anything.
    stores.clock.advance(LEASE)
    reclaimed = transitions.claim_due_simulation_calls(limit=10, lease=LEASE)
    assert reclaimed[0].runs[0].check_count == 2


def test_concurrent_instances_claim_disjoint_calls(
    migrated_database, public_api_rows
) -> None:
    clock = ManualClock()
    stores = clocked_stores(migrated_database, clock)
    simulations = simulation_store(migrated_database)
    run_ids = set()
    for index in range(12):
        simulation = simulations.create(
            household_simulation(public_api_rows, legacy_simulation_id=index + 1)
        )
        run = stores.runs.start_simulation_run(
            household_run(simulation, public_api_rows)
        ).run
        stores.transitions.claim_simulation_dispatch([run.id])
        stores.transitions.record_simulation_dispatched(
            [run.id], modal_call_id=f"fc-{index}", first_check_at=clock()
        )
        run_ids.add(run.id)
    instances = 4
    barrier = Barrier(instances)

    def claim(_instance: int) -> list[str]:
        transitions = RunTransitions(
            create_simulation_engine(migrated_database.runtime_url), clock=clock
        )
        try:
            barrier.wait()
            return [
                call.modal_call_id
                for call in transitions.claim_due_simulation_calls(limit=5, lease=LEASE)
            ]
        finally:
            transitions.engine.dispose()

    with ThreadPoolExecutor(max_workers=instances) as pool:
        claims = list(pool.map(claim, range(instances)))

    claimed_calls = [call for instance_claims in claims for call in instance_claims]
    assert len(claimed_calls) == len(set(claimed_calls)) == len(run_ids)


def test_results_are_written_in_the_transaction_that_marks_success(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    run = _running_run(stores, migrated_database, public_api_rows)

    def failing_writer(_connection, _runs) -> None:
        msg = "the result rows could not be written"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="result rows"):
        stores.transitions.complete_simulation_call("fc-economy", failing_writer)

    assert stores.runs.get_simulation_run(run.id).status is RunStatus.RUNNING
    assert stores.transitions.complete_simulation_call("fc-economy", _no_results)
    succeeded = stores.runs.get_simulation_run(run.id)
    assert succeeded.status is RunStatus.SUCCEEDED
    assert succeeded.completed_at == stores.clock()


def test_a_finished_call_is_recorded_once(migrated_database, public_api_rows) -> None:
    stores = clocked_stores(migrated_database)
    _running_run(stores, migrated_database, public_api_rows)
    calls = []

    assert stores.transitions.complete_simulation_call("fc-economy", _no_results)

    assert not stores.transitions.complete_simulation_call(
        "fc-economy", lambda _connection, runs: calls.append(runs)
    )
    assert calls == []
    assert not stores.transitions.fail_simulation_call("fc-economy", WORKER_ERROR)


def test_a_failed_call_records_a_bounded_error(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    run = _running_run(stores, migrated_database, public_api_rows)

    assert stores.transitions.fail_simulation_call(
        "fc-economy", RunError(RunErrorCode.WORKER_ERROR, "x" * 5000)
    )

    failed = stores.runs.get_simulation_run(run.id)
    assert failed.status is RunStatus.FAILED
    assert failed.error_code == "worker_error"
    assert len(failed.error_message) == 2000
    assert failed.next_check_at is None


def _economy_report_run(stores, database, rows):
    baseline, reform = economy_pair(database, rows)
    report = definition_report_store(database).create(economy_report(baseline, reform))
    runs = [
        stores.runs.start_simulation_run(economy_run(simulation, rows)).run
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
    return report_run, runs


def _succeed(stores, run, call_id) -> None:
    stores.transitions.claim_simulation_dispatch([run.id])
    stores.transitions.record_simulation_dispatched(
        [run.id], modal_call_id=call_id, first_check_at=stores.clock()
    )
    assert stores.transitions.complete_simulation_call(call_id, _no_results)


def test_a_comparison_starts_only_after_both_runs_succeed(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    report_run, (baseline_run, reform_run) = _economy_report_run(
        stores, migrated_database, public_api_rows
    )

    assert not stores.transitions.claim_comparison_dispatch(report_run.id)
    _succeed(stores, baseline_run, "fc-baseline")
    assert not stores.transitions.claim_comparison_dispatch(report_run.id)
    _succeed(stores, reform_run, "fc-reform")

    assert stores.transitions.claim_comparison_dispatch(report_run.id)
    assert not stores.transitions.claim_comparison_dispatch(report_run.id)
    assert (
        stores.runs.get_report_run(report_run.id).comparison_status
        is RunStatus.DISPATCHING
    )


def test_a_comparison_runs_and_records_its_results_once(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    report_run, (baseline_run, reform_run) = _economy_report_run(
        stores, migrated_database, public_api_rows
    )
    _succeed(stores, baseline_run, "fc-baseline")
    _succeed(stores, reform_run, "fc-reform")
    transitions = stores.transitions
    transitions.claim_comparison_dispatch(report_run.id)
    written = []

    assert transitions.record_comparison_dispatched(
        report_run.id, modal_call_id="fc-comparison", first_check_at=stores.clock()
    )
    claimed = transitions.claim_due_comparisons(limit=10, lease=LEASE)
    assert [due.id for due in claimed] == [report_run.id]
    assert claimed[0].comparison_check_count == 1
    assert transitions.claim_due_comparisons(limit=10, lease=LEASE) == []
    assert transitions.complete_comparison(
        report_run.id, lambda _connection, record: written.append(record)
    )

    assert [record.id for record in written] == [report_run.id]
    finished = stores.runs.get_report_run(report_run.id)
    assert finished.comparison_status is RunStatus.SUCCEEDED
    assert finished.comparison_modal_call_id == "fc-comparison"
    assert finished.comparison_next_check_at is None
    assert not transitions.complete_comparison(report_run.id, _no_results)
    assert not transitions.fail_comparison(report_run.id, WORKER_ERROR)


def test_a_comparison_failure_is_recorded(migrated_database, public_api_rows) -> None:
    stores = clocked_stores(migrated_database)
    report_run, (baseline_run, reform_run) = _economy_report_run(
        stores, migrated_database, public_api_rows
    )
    _succeed(stores, baseline_run, "fc-baseline")
    _succeed(stores, reform_run, "fc-reform")
    transitions = stores.transitions
    transitions.claim_comparison_dispatch(report_run.id)
    transitions.record_comparison_dispatched(
        report_run.id, modal_call_id="fc-comparison", first_check_at=stores.clock()
    )

    assert transitions.fail_comparison(report_run.id, WORKER_ERROR)

    failed = stores.runs.get_report_run(report_run.id)
    assert failed.comparison_status is RunStatus.FAILED
    assert failed.comparison_error_code == "worker_error"
    assert failed.comparison_completed_at == stores.clock()


def test_a_comparison_says_whether_it_recorded_a_call(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    report_run, (baseline_run, reform_run) = _economy_report_run(
        stores, migrated_database, public_api_rows
    )
    _succeed(stores, baseline_run, "fc-baseline")
    _succeed(stores, reform_run, "fc-reform")
    transitions = stores.transitions
    transitions.claim_comparison_dispatch(report_run.id)

    assert not transitions.comparison_holds_call(report_run.id, "fc-comparison")

    transitions.record_comparison_dispatched(
        report_run.id, modal_call_id="fc-comparison", first_check_at=stores.clock()
    )

    assert transitions.comparison_holds_call(report_run.id, "fc-comparison")
    assert not transitions.comparison_holds_call(report_run.id, "fc-other")


def test_a_refused_comparison_dispatch_fails_the_step(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    report_run, (baseline_run, reform_run) = _economy_report_run(
        stores, migrated_database, public_api_rows
    )
    _succeed(stores, baseline_run, "fc-baseline")
    _succeed(stores, reform_run, "fc-reform")
    stores.transitions.claim_comparison_dispatch(report_run.id)

    assert stores.transitions.record_comparison_dispatch_failure(
        report_run.id, RunError(RunErrorCode.DISPATCH_FAILED, "Modal refused it.")
    )

    failed = stores.runs.get_report_run(report_run.id)
    assert failed.comparison_status is RunStatus.FAILED
    assert failed.comparison_error_code == "dispatch_failed"
    assert failed.comparison_modal_call_id is None


def test_household_report_runs_have_no_comparison_to_claim(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    baseline_run, reform_run = _household_pair(
        stores, migrated_database, public_api_rows
    )
    baseline = simulation_store(migrated_database).get(baseline_run.simulation_id)
    reform = simulation_store(migrated_database).get(reform_run.simulation_id)
    report = definition_report_store(migrated_database).create(
        economy_report(baseline, reform, kind=ReportKind.HOUSEHOLD)
    )
    report_run = stores.runs.create_report_run(
        NewReportRun(
            report_id=report.id,
            baseline_simulation_run_id=baseline_run.id,
            reform_simulation_run_id=reform_run.id,
            trigger=RunTrigger.INITIAL,
        )
    )

    assert not stores.transitions.claim_comparison_dispatch(report_run.id)


def test_dispatches_left_for_ten_minutes_are_failed(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    report_run, (baseline_run, reform_run) = _economy_report_run(
        stores, migrated_database, public_api_rows
    )
    _succeed(stores, baseline_run, "fc-baseline")
    _succeed(stores, reform_run, "fc-reform")
    stores.transitions.claim_comparison_dispatch(report_run.id)
    stalled = _pending_run(stores, migrated_database, public_api_rows)
    stores.transitions.claim_simulation_dispatch([stalled.id])
    stores.clock.advance(timedelta(minutes=10, seconds=1))
    recent = household_run(
        household_baseline(migrated_database, public_api_rows), public_api_rows
    )
    recent_run = stores.runs.start_simulation_run(recent).run
    stores.transitions.claim_simulation_dispatch([recent_run.id])

    abandoned = stores.transitions.fail_abandoned_dispatches(
        claimed_before=stores.clock() - timedelta(minutes=10)
    )

    assert abandoned.simulation_run_ids == (stalled.id,)
    assert abandoned.report_run_ids == (report_run.id,)
    failed_run = stores.runs.get_simulation_run(stalled.id)
    assert failed_run.status is RunStatus.FAILED
    assert failed_run.error_code == "dispatch_abandoned"
    # The claimer may have stopped, or may not know whether its record
    # committed; the message says only what is known.
    assert failed_run.error_message == (
        "No Modal call was recorded for the dispatch in time."
    )
    assert (
        stores.runs.get_report_run(report_run.id).comparison_error_code
        == "dispatch_abandoned"
    )
    assert stores.runs.get_simulation_run(recent_run.id).status is RunStatus.DISPATCHING


def test_a_comparison_of_a_failed_run_fails_too(
    migrated_database, public_api_rows
) -> None:
    stores = clocked_stores(migrated_database)
    report_run, (baseline_run, reform_run) = _economy_report_run(
        stores, migrated_database, public_api_rows
    )
    waiting, _ = _economy_report_run(stores, migrated_database, public_api_rows)
    _succeed(stores, baseline_run, "fc-baseline")
    stores.transitions.claim_simulation_dispatch([reform_run.id])
    stores.transitions.record_simulation_dispatched(
        [reform_run.id], modal_call_id="fc-reform", first_check_at=stores.clock()
    )
    stores.transitions.fail_simulation_call("fc-reform", WORKER_ERROR)

    failed = stores.transitions.fail_comparisons_of_failed_runs()

    assert failed == (report_run.id,)
    record = stores.runs.get_report_run(report_run.id)
    assert record.comparison_status is RunStatus.FAILED
    assert record.comparison_error_code == "simulation_run_failed"
    assert stores.runs.get_report_run(waiting.id).comparison_status is (
        RunStatus.PENDING
    )
    assert stores.transitions.fail_comparisons_of_failed_runs() == ()
