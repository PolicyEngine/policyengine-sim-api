"""Starting, finishing and settling budget-window runs, as the Modal worker role.

A budget window's years are report runs naming its budget-window run. The
simulation service creates them all pending and starts the first ones; each
year's coordinator, when its year ends, starts the earliest years still
waiting, keeping at most five under way, and the last year to end marks the
window. These run against a disposable Postgres that the Public API's v2
migrations created (see ``report_run_database``).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from budget_window_database import BudgetWindowSeeder, SeededWindow
from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_stage12_persistence import (
    BUDGET_WINDOW_MAX_RUNNING_YEARS,
    ActiveRun,
    BudgetWindowStore,
    ReportRunStore,
    RunKind,
    RunStatus,
    WindowAdvance,
    YearStartDeferred,
    YearToStart,
    budget_window_store,
)
from report_run_database import NOW, SEED_URL_ENV, worker_database_url
from sqlalchemy import Engine, create_engine, event

# The simulation service's role, which the Public API's manifest grants.
SERVICE_ROLE = "policyengine_v2_simulation_service"

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from report_run_database import ReportRunSeeder, SeededReportRun

LATER = NOW + timedelta(minutes=5)
ALL_YEARS = range(2026, 2033)


class RecordingStarter:
    """Starts a year by recording it and returning a new call ID.

    A year whose report run is in ``refused`` raises, as Modal does when it
    refuses a call. ``during_start`` runs before the call ID is returned.
    """

    def __init__(self, *, refused=(), delay_seconds=0.0, during_start=None):
        self.started: list[UUID] = []
        self.years: list[YearToStart] = []
        self.refused = set(refused)
        self.delay_seconds = delay_seconds
        self.during_start = during_start
        self._lock = threading.Lock()

    def __call__(self, year_to_start: YearToStart) -> str:
        report_run_id = year_to_start.run_ids.report_run_id
        if report_run_id in self.refused:
            msg = "Modal refused the call"
            raise RuntimeError(msg)
        time.sleep(self.delay_seconds)
        if self.during_start is not None:
            self.during_start(report_run_id)
        with self._lock:
            self.started.append(report_run_id)
            self.years.append(year_to_start)
            return f"fc-started-{len(self.started)}"


@pytest.fixture
def store() -> Iterator[BudgetWindowStore]:
    window_store = BudgetWindowStore(worker_database_url())
    try:
        yield window_store
    finally:
        window_store.engine.dispose()


@pytest.fixture
def windows(seeder: ReportRunSeeder) -> BudgetWindowSeeder:
    return BudgetWindowSeeder(seeder)


@pytest.fixture
def window(windows: BudgetWindowSeeder) -> Iterator[SeededWindow]:
    """Yield a pending window run with seven pending years, 2026 to 2032."""
    seeded = windows.seed_window(first_year=2026, size=7)
    try:
        yield seeded
    finally:
        windows.remove(seeded)


def _run_ids(window: SeededWindow, years) -> list[UUID]:
    return [window.year(year).report_run_id for year in years]


def _succeeded(windows, window, years) -> None:
    windows.set_years(
        window,
        years,
        status="succeeded",
        modal_call_id="fc-finished",
        started_at=NOW,
        completed_at=NOW,
    )


def test_the_window_runs_at_most_five_years_at_once() -> None:
    assert BUDGET_WINDOW_MAX_RUNNING_YEARS == 5


def test_the_first_advance_starts_the_five_earliest_years(
    store, windows, window
) -> None:
    starter = RecordingStarter()

    advance = store.advance_window(window.window_run_id, now=LATER, start_year=starter)

    assert starter.started == _run_ids(window, range(2026, 2031))
    assert advance == WindowAdvance(
        status=RunStatus.PENDING,
        changed=False,
        started_years=(2026, 2027, 2028, 2029, 2030),
    )
    rows = windows.year_rows(window)
    assert [rows[year]["modal_call_id"] for year in ALL_YEARS] == [
        "fc-started-1",
        "fc-started-2",
        "fc-started-3",
        "fc-started-4",
        "fc-started-5",
        None,
        None,
    ]
    assert {rows[year]["status"] for year in ALL_YEARS} == {"pending"}
    assert [rows[year]["updated_at"] for year in range(2026, 2031)] == [LATER] * 5
    assert windows.window_row(window)["status"] == "pending"


def test_the_starter_is_given_each_year_and_its_runs(store, window) -> None:
    starter = RecordingStarter()

    store.advance_window(
        window.window_run_id, now=LATER, start_year=starter, max_running_years=2
    )

    assert starter.years == [
        YearToStart(
            year=year,
            run_ids=ReportRunIds(
                report_run_id=window.year(year).report_run_id,
                baseline_simulation_run_id=window.year(year).baseline_simulation_run_id,
                reform_simulation_run_id=window.year(year).reform_simulation_run_id,
            ),
        )
        for year in (2026, 2027)
    ]


def test_an_advance_fills_only_the_free_places(store, windows, window) -> None:
    _succeeded(windows, window, [2026])
    windows.set_years(window, [2027, 2028], status="running", modal_call_id="fc-run")
    # Started, but its coordinator has not claimed it yet.
    windows.set_years(window, [2029], modal_call_id="fc-queued")
    starter = RecordingStarter()

    advance = store.advance_window(window.window_run_id, now=LATER, start_year=starter)

    assert starter.started == _run_ids(window, [2030, 2031])
    assert advance.started_years == (2030, 2031)
    assert windows.year_rows(window)[2032]["modal_call_id"] is None


def test_an_advance_without_a_starter_starts_nothing(store, windows, window) -> None:
    advance = store.advance_window(window.window_run_id, now=LATER)

    assert advance == WindowAdvance(
        status=RunStatus.PENDING, changed=False, started_years=()
    )
    assert {row["modal_call_id"] for row in windows.year_rows(window).values()} == {
        None
    }


def test_a_failed_year_fails_the_window_its_waiting_years_and_their_simulations(
    store, windows, window
) -> None:
    windows.set_window(window, status="running", started_at=NOW)
    windows.set_years(
        window,
        [2026],
        status="failed",
        modal_call_id="fc-failed",
        error_message="worker_failed: out of memory",
        completed_at=NOW,
    )
    windows.set_years(window, [2027], status="running", modal_call_id="fc-running")
    starter = RecordingStarter()

    advance = store.advance_window(window.window_run_id, now=LATER, start_year=starter)

    assert starter.started == []
    assert advance == WindowAdvance(
        status=RunStatus.FAILED, changed=True, started_years=()
    )
    window_row = windows.window_row(window)
    assert window_row["status"] == "failed"
    assert window_row["error_message"].startswith("budget_window_year_failed: ")
    assert "2026" in window_row["error_message"]
    assert (window_row["completed_at"], window_row["updated_at"]) == (LATER, LATER)
    rows = windows.year_rows(window)
    # The running year is its coordinator's to finish.
    assert (rows[2027]["status"], rows[2027]["completed_at"]) == ("running", None)
    for year in range(2028, 2033):
        assert rows[year]["status"] == "failed"
        assert rows[year]["error_message"].startswith("budget_window_failed: ")
        assert rows[year]["completed_at"] == LATER
        assert windows.simulation_run_statuses(window.year(year)) == (
            "failed",
            "failed",
        )
    assert windows.simulation_run_statuses(window.year(2027)) == (
        "pending",
        "pending",
    )


def test_the_last_year_to_succeed_marks_the_window_succeeded(
    store, windows, window
) -> None:
    windows.set_window(window, status="running", started_at=NOW)
    _succeeded(windows, window, ALL_YEARS)

    advance = store.advance_window(
        window.window_run_id, now=LATER, start_year=RecordingStarter()
    )

    assert advance == WindowAdvance(
        status=RunStatus.SUCCEEDED, changed=True, started_years=()
    )
    window_row = windows.window_row(window)
    assert (window_row["status"], window_row["error_message"]) == ("succeeded", None)
    assert (window_row["completed_at"], window_row["updated_at"]) == (LATER, LATER)


def test_a_window_with_a_year_still_running_stays_as_it_is(
    store, windows, window
) -> None:
    windows.set_window(window, status="running", started_at=NOW)
    _succeeded(windows, window, range(2026, 2032))
    windows.set_years(window, [2032], status="running", modal_call_id="fc-last")

    advance = store.advance_window(
        window.window_run_id, now=LATER, start_year=RecordingStarter()
    )

    assert advance == WindowAdvance(
        status=RunStatus.RUNNING, changed=False, started_years=()
    )
    assert windows.window_row(window)["completed_at"] is None


def test_a_refused_start_fails_that_year_and_the_window(store, windows, window) -> None:
    starter = RecordingStarter(refused=_run_ids(window, [2028]))

    advance = store.advance_window(window.window_run_id, now=LATER, start_year=starter)

    assert starter.started == _run_ids(window, [2026, 2027])
    assert advance == WindowAdvance(
        status=RunStatus.FAILED, changed=True, started_years=(2026, 2027)
    )
    rows = windows.year_rows(window)
    assert [rows[year]["modal_call_id"] for year in (2026, 2027)] == [
        "fc-started-1",
        "fc-started-2",
    ]
    assert rows[2028]["status"] == "failed"
    assert rows[2028]["error_message"].startswith("modal_call_refused: ")
    # The cause's type, never its text, names why the start failed.
    assert "RuntimeError" in rows[2028]["error_message"]
    assert "Modal refused the call" not in rows[2028]["error_message"]
    assert {rows[year]["status"] for year in range(2029, 2033)} == {"failed"}
    window_row = windows.window_row(window)
    assert window_row["status"] == "failed"
    assert "2028" in window_row["error_message"]


def test_a_refused_start_names_its_cause_on_its_own_simulations(
    store, windows, window, caplog
) -> None:
    starter = RecordingStarter(refused=_run_ids(window, [2026]))

    with caplog.at_level(logging.WARNING, logger=budget_window_store.__name__):
        store.advance_window(window.window_run_id, now=LATER, start_year=starter)

    for seeded_year, message_start in (
        (window.year(2026), "modal_call_refused: "),
        (window.year(2027), "budget_window_failed: "),
    ):
        for simulation_run_id in (
            seeded_year.baseline_simulation_run_id,
            seeded_year.reform_simulation_run_id,
        ):
            row = windows.seeder.row("simulation_runs", simulation_run_id)
            assert row["status"] == "failed"
            assert row["error_message"].startswith(message_start)
    # The cause's type is logged; its text may hold connection details.
    assert "RuntimeError" in caplog.text
    assert str(window.year(2026).report_run_id) in caplog.text
    assert "Modal refused the call" not in caplog.text


def test_the_service_role_starts_and_fails_a_window(windows, window) -> None:
    # The simulation service starts a window's first years with the same
    # store, as its own role, and fails the window when Modal refuses one.
    service_store = BudgetWindowStore(engine=_engine_as_the_service(windows))
    try:
        starter = RecordingStarter(refused=_run_ids(window, [2028]))

        advance = service_store.advance_window(
            window.window_run_id, now=LATER, start_year=starter
        )
    finally:
        service_store.engine.dispose()

    assert advance.started_years == (2026, 2027)
    assert windows.window_row(window)["status"] == "failed"


def _engine_as_the_service(windows: BudgetWindowSeeder) -> Engine:
    """Return an engine whose every connection acts as the service's role."""
    engine = create_engine(os.environ[SEED_URL_ENV])

    @event.listens_for(engine, "connect")
    def act_as_the_service(dbapi_connection, _connection_record) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f"SET ROLE {SERVICE_ROLE}")

    return engine


def test_a_year_started_late_counts_from_its_start(run_store, windows, window) -> None:
    # The service created every year at submit; an earlier year's coordinator
    # recorded this year's call a day later, and Modal hasn't run it yet.
    started_late_at = NOW + timedelta(hours=30)
    windows.set_window(window, status="running", started_at=NOW)
    windows.set_years(
        window, [2031], modal_call_id="fc-queued", updated_at=started_late_at
    )

    listed = {
        run.run_id
        for run in run_store.list_active_runs(
            RunKind.REPORT_RUN, active_since_before=NOW + timedelta(hours=25)
        )
    }

    assert window.year(2031).report_run_id not in listed


def test_a_start_that_fails_after_its_call_claimed_the_year_keeps_the_year(
    store, windows, window
) -> None:
    # The service waits a limited time for Modal to accept a call; Modal may
    # accept it later, and its coordinator claim the year, before the wait's
    # end is recorded.
    first_year_run_id = window.year(2026).report_run_id

    def claim_then_time_out(year_to_start: YearToStart) -> str:
        if year_to_start.run_ids.report_run_id == first_year_run_id:
            windows.set_years(window, [2026], status="running", modal_call_id="fc-late")
            raise TimeoutError
        return f"fc-{year_to_start.year}"

    advance = store.advance_window(
        window.window_run_id, now=LATER, start_year=claim_then_time_out
    )

    assert advance == WindowAdvance(
        status=RunStatus.PENDING,
        changed=False,
        started_years=(2026, 2027, 2028, 2029, 2030),
    )
    rows = windows.year_rows(window)
    assert (rows[2026]["status"], rows[2026]["modal_call_id"]) == ("running", "fc-late")
    assert windows.window_row(window)["status"] == "pending"


def test_a_deferred_start_leaves_that_year_and_the_later_ones_waiting(
    store, windows, window
) -> None:
    def defer_from_2028(year_to_start: YearToStart) -> str:
        if year_to_start.year >= 2028:
            raise YearStartDeferred
        return f"fc-{year_to_start.year}"

    advance = store.advance_window(
        window.window_run_id, now=LATER, start_year=defer_from_2028
    )

    assert advance == WindowAdvance(
        status=RunStatus.PENDING, changed=False, started_years=(2026, 2027)
    )
    rows = windows.year_rows(window)
    assert {rows[year]["status"] for year in ALL_YEARS} == {"pending"}
    assert [rows[year]["modal_call_id"] for year in ALL_YEARS] == [
        "fc-2026",
        "fc-2027",
        None,
        None,
        None,
        None,
        None,
    ]
    assert windows.window_row(window)["status"] == "pending"


def test_a_finished_window_is_left_as_it_is(store, windows, window) -> None:
    windows.set_window(
        window, status="failed", error_message="earlier", completed_at=NOW
    )
    starter = RecordingStarter()

    advance = store.advance_window(window.window_run_id, now=LATER, start_year=starter)

    assert starter.started == []
    assert advance == WindowAdvance(
        status=RunStatus.FAILED, changed=False, started_years=()
    )
    assert windows.window_row(window)["error_message"] == "earlier"


def test_a_year_its_coordinator_claimed_during_the_start_keeps_its_claim(
    store, windows, window
) -> None:
    def claim_while_starting(report_run_id: UUID) -> None:
        # The started call claims its year before the advance records it.
        if report_run_id == window.year(2026).report_run_id:
            windows.set_years(
                window, [2026], status="running", modal_call_id="fc-claimed"
            )

    starter = RecordingStarter(during_start=claim_while_starting)

    store.advance_window(window.window_run_id, now=LATER, start_year=starter)

    row = windows.year_rows(window)[2026]
    assert (row["status"], row["modal_call_id"]) == ("running", "fc-claimed")


def test_two_advances_at_once_start_each_year_once(store, windows, window) -> None:
    starter = RecordingStarter(delay_seconds=0.2)
    barrier = Barrier(2)

    def advance() -> WindowAdvance:
        barrier.wait()
        return store.advance_window(window.window_run_id, now=LATER, start_year=starter)

    with ThreadPoolExecutor(max_workers=2) as pool:
        advances = list(pool.map(lambda _: advance(), range(2)))

    assert sorted(starter.started) == sorted(_run_ids(window, range(2026, 2031)))
    assert sorted(year for finished in advances for year in finished.started_years) == [
        2026,
        2027,
        2028,
        2029,
        2030,
    ]


def test_an_unknown_window_raises_lookup_error(store) -> None:
    with pytest.raises(LookupError, match="budget-window run"):
        store.advance_window(uuid4(), now=LATER)


def test_a_claimed_year_marks_its_window_running_once(store, windows, window) -> None:
    assert store.start_window(window.year(2026).report_run_id, started_at=LATER)
    assert not store.start_window(
        window.year(2027).report_run_id, started_at=LATER + timedelta(hours=1)
    )

    window_row = windows.window_row(window)
    assert window_row["status"] == "running"
    assert (window_row["started_at"], window_row["updated_at"]) == (LATER, LATER)


def test_a_report_run_outside_any_window_marks_nothing(
    store, seeded_run: SeededReportRun
) -> None:
    assert not store.start_window(seeded_run.run_ids.report_run_id, started_at=LATER)


def test_a_year_names_its_window(store, window, seeded_run) -> None:
    assert store.window_of_year(window.year(2029).report_run_id) == (
        window.window_run_id
    )
    assert store.window_of_year(seeded_run.run_ids.report_run_id) is None
    assert store.window_of_year(uuid4()) is None


def test_an_idle_window_with_waiting_years_fails_as_stalled(
    store, windows, window
) -> None:
    windows.set_window(window, status="running", started_at=NOW)
    _succeeded(windows, window, range(2026, 2031))
    cutoff = NOW + timedelta(minutes=10)

    assert window.window_run_id in store.list_idle_windows(idle_since_before=cutoff)
    settled = store.settle_idle_window(
        window.window_run_id, idle_since_before=cutoff, now=LATER
    )

    assert settled == WindowAdvance(
        status=RunStatus.FAILED, changed=True, started_years=()
    )
    window_row = windows.window_row(window)
    assert window_row["error_message"].startswith("budget_window_stalled: ")
    rows = windows.year_rows(window)
    for year in (2031, 2032):
        assert rows[year]["status"] == "failed"
        # The years record the stall, not another year's failure.
        assert rows[year]["error_message"].startswith("budget_window_stalled: ")
        for simulation_run_id in (
            window.year(year).baseline_simulation_run_id,
            window.year(year).reform_simulation_run_id,
        ):
            assert windows.seeder.row("simulation_runs", simulation_run_id)[
                "error_message"
            ].startswith("budget_window_stalled: ")
    assert window.window_run_id not in store.list_idle_windows(idle_since_before=cutoff)


def test_an_idle_window_whose_years_all_succeeded_is_marked_succeeded(
    store, windows, window
) -> None:
    windows.set_window(window, status="running", started_at=NOW)
    _succeeded(windows, window, ALL_YEARS)
    cutoff = NOW + timedelta(minutes=10)

    assert window.window_run_id in store.list_idle_windows(idle_since_before=cutoff)
    settled = store.settle_idle_window(
        window.window_run_id, idle_since_before=cutoff, now=LATER
    )

    assert settled.status is RunStatus.SUCCEEDED
    assert windows.window_row(window)["status"] == "succeeded"


def test_a_window_whose_service_started_no_year_is_idle(store, window) -> None:
    # The simulation service stopped after creating the window's rows.
    cutoff = NOW + timedelta(minutes=10)

    assert window.window_run_id in store.list_idle_windows(idle_since_before=cutoff)
    assert (
        store.settle_idle_window(
            window.window_run_id, idle_since_before=cutoff, now=LATER
        ).status
        is RunStatus.FAILED
    )


def test_a_window_with_a_year_under_way_is_not_idle(store, windows, window) -> None:
    windows.set_years(window, [2026], modal_call_id="fc-queued")
    cutoff = NOW + timedelta(minutes=10)

    assert window.window_run_id not in store.list_idle_windows(idle_since_before=cutoff)
    assert store.settle_idle_window(
        window.window_run_id, idle_since_before=cutoff, now=LATER
    ) == WindowAdvance(status=RunStatus.PENDING, changed=False, started_years=())


def test_a_window_whose_year_changed_recently_is_not_idle(
    store, windows, window
) -> None:
    windows.set_window(window, status="running", started_at=NOW)
    _succeeded(windows, window, [2026])
    windows.set_years(window, [2026], updated_at=NOW + timedelta(minutes=20))
    cutoff = NOW + timedelta(minutes=10)

    assert window.window_run_id not in store.list_idle_windows(idle_since_before=cutoff)
    assert not store.settle_idle_window(
        window.window_run_id, idle_since_before=cutoff, now=LATER
    ).changed


@pytest.fixture
def run_store() -> Iterator[ReportRunStore]:
    report_run_store = ReportRunStore(worker_database_url())
    try:
        yield report_run_store
    finally:
        report_run_store.engine.dispose()


def _listed_report_runs(run_store: ReportRunStore) -> dict[UUID, ActiveRun]:
    return {
        run.run_id: run
        for run in run_store.list_active_runs(
            RunKind.REPORT_RUN, active_since_before=NOW + timedelta(minutes=30)
        )
    }


def test_the_sweep_lists_a_window_s_started_years_but_not_its_waiting_ones(
    run_store, windows, window
) -> None:
    windows.set_window(window, status="running", started_at=NOW)
    windows.set_years(
        window, [2026], status="running", modal_call_id="fc-running", started_at=NOW
    )
    windows.set_years(window, [2027], modal_call_id="fc-queued")

    listed = _listed_report_runs(run_store)

    for year in (2026, 2027):
        run = listed[window.year(year).report_run_id]
        assert run.budget_window_run_id == window.window_run_id
    assert not set(_run_ids(window, range(2028, 2033))) & set(listed)


def test_the_sweep_lists_the_waiting_years_of_a_finished_window(
    run_store, windows, window
) -> None:
    windows.set_window(window, status="failed", error_message="x", completed_at=NOW)

    listed = _listed_report_runs(run_store)

    assert set(_run_ids(window, ALL_YEARS)) <= set(listed)
