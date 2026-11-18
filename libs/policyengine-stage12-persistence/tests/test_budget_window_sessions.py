"""The budget-window store on ORM sessions, as the Modal worker role."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Lock
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from budget_window_database import BudgetWindowSeeder
from policyengine_stage12_persistence import (
    BudgetWindowStore,
    RunStatus,
    WindowAdvance,
    YearStartDeferred,
)
from report_run_database import NOW

if TYPE_CHECKING:
    from collections.abc import Iterator

    from budget_window_database import SeededWindow
    from policyengine_stage12_persistence import YearToStart
    from report_run_database import ReportRunSeeder
    from session_database import StatementLog
    from sqlalchemy.orm import Session, sessionmaker

LATER = NOW + timedelta(minutes=5)
IDLE_CUTOFF = NOW + timedelta(minutes=1)


class CountingStarter:
    """Start each year by returning a new call ID, recording which years started."""

    def __init__(self) -> None:
        self.started: list[int] = []
        self._lock = Lock()

    def __call__(self, year_to_start: YearToStart) -> str:
        with self._lock:
            self.started.append(year_to_start.year)
            return f"fc-started-{year_to_start.year}-{len(self.started)}"


@pytest.fixture
def windows(seeder: ReportRunSeeder) -> BudgetWindowSeeder:
    return BudgetWindowSeeder(seeder)


@pytest.fixture
def window(windows: BudgetWindowSeeder) -> Iterator[SeededWindow]:
    seeded = windows.seed_window(first_year=2026, size=7)
    try:
        yield seeded
    finally:
        windows.remove(seeded)


@pytest.fixture
def store(
    worker_sessions: sessionmaker[Session], statements: StatementLog
) -> BudgetWindowStore:
    assert statements.sent == []
    return BudgetWindowStore(session_factory=worker_sessions)


def test_a_year_names_its_window(store, window) -> None:
    year = window.year(2026)

    assert store.window_of_year(year.report_run_id) == window.window_run_id
    assert store.window_of_year(uuid4()) is None


def test_the_first_claimed_year_starts_its_window_once(store, windows, window) -> None:
    report_run_id = window.year(2026).report_run_id

    first = store.start_window(report_run_id, started_at=NOW)
    second = store.start_window(report_run_id, started_at=LATER)

    assert (first, second) == (True, False)
    stored = windows.window_row(window)
    assert stored["status"] == "running"
    assert stored["started_at"] == NOW


def test_an_advance_starts_the_earliest_waiting_years(
    store, statements, windows, window
) -> None:
    starter = CountingStarter()

    advance = store.advance_window(window.window_run_id, now=LATER, start_year=starter)

    assert advance == WindowAdvance(
        status=RunStatus.PENDING,
        changed=False,
        started_years=(2026, 2027, 2028, 2029, 2030),
    )
    assert starter.started == [2026, 2027, 2028, 2029, 2030]
    years = windows.year_rows(window)
    assert years[2026]["modal_call_id"].startswith("fc-started-2026")
    assert years[2031]["modal_call_id"] is None
    assert statements.commits == 1
    [lock] = [sent for sent in statements.sent if sent.endswith("FOR UPDATE")]
    assert "public.budget_window_runs" in lock


def test_concurrent_advances_start_each_year_once(
    worker_sessions, statements, windows, window
) -> None:
    assert statements.sent == []
    advancers = 4
    start_together = Barrier(advancers)
    starter = CountingStarter()

    def advance(_advancer: int) -> WindowAdvance:
        store = BudgetWindowStore(session_factory=worker_sessions)
        start_together.wait(timeout=10)
        return store.advance_window(window.window_run_id, now=LATER, start_year=starter)

    with ThreadPoolExecutor(max_workers=advancers) as executor:
        list(executor.map(advance, range(advancers)))

    assert sorted(starter.started) == [2026, 2027, 2028, 2029, 2030]
    started = [
        year
        for year, row in windows.year_rows(window).items()
        if row["modal_call_id"] is not None
    ]
    assert sorted(started) == [2026, 2027, 2028, 2029, 2030]


def test_a_refused_start_fails_the_year_and_the_window(store, windows, window) -> None:
    def refuse(_year_to_start: YearToStart) -> str:
        message = "Modal refused the call"
        raise RuntimeError(message)

    advance = store.advance_window(window.window_run_id, now=LATER, start_year=refuse)

    assert advance == WindowAdvance(status=RunStatus.FAILED, changed=True)
    assert windows.window_row(window)["status"] == "failed"
    years = windows.year_rows(window)
    assert years[2026]["error_message"].startswith("modal_call_refused: ")
    assert years[2027]["error_message"].startswith("budget_window_failed: ")
    assert windows.simulation_run_statuses(window.year(2027)) == ("failed", "failed")


def test_a_deferred_start_leaves_the_years_waiting(store, windows, window) -> None:
    def defer(_year_to_start: YearToStart) -> str:
        raise YearStartDeferred

    advance = store.advance_window(window.window_run_id, now=LATER, start_year=defer)

    assert advance == WindowAdvance(status=RunStatus.PENDING, changed=False)
    assert windows.year_rows(window)[2026]["status"] == "pending"


def test_every_year_succeeded_marks_the_window_succeeded(
    store, windows, window
) -> None:
    windows.set_years(window, range(2026, 2033), status="succeeded")

    first = store.advance_window(window.window_run_id, now=LATER)
    second = store.advance_window(window.window_run_id, now=LATER)

    assert first == WindowAdvance(status=RunStatus.SUCCEEDED, changed=True)
    assert second == WindowAdvance(status=RunStatus.SUCCEEDED, changed=False)
    assert windows.window_row(window)["completed_at"] == LATER


def test_a_failed_year_fails_the_window_with_its_waiting_years(
    store, windows, window
) -> None:
    windows.set_years(window, [2027], status="failed")

    advance = store.advance_window(window.window_run_id, now=LATER)

    assert advance == WindowAdvance(status=RunStatus.FAILED, changed=True)
    stored = windows.window_row(window)
    assert stored["error_message"].startswith("budget_window_year_failed: ")
    assert "2027" in stored["error_message"]
    assert windows.year_rows(window)[2026]["status"] == "failed"


def test_a_missing_window_cannot_be_advanced(store) -> None:
    with pytest.raises(LookupError, match="does not exist"):
        store.advance_window(uuid4(), now=LATER)


def test_an_idle_window_is_listed_and_settled_as_stalled(
    store, windows, window
) -> None:
    listed = store.list_idle_windows(idle_since_before=IDLE_CUTOFF)

    settled = store.settle_idle_window(
        window.window_run_id, idle_since_before=IDLE_CUTOFF, now=LATER
    )
    repeated = store.settle_idle_window(
        window.window_run_id, idle_since_before=IDLE_CUTOFF, now=LATER
    )

    assert window.window_run_id in listed
    assert settled == WindowAdvance(status=RunStatus.FAILED, changed=True)
    assert repeated == WindowAdvance(status=RunStatus.FAILED, changed=False)
    assert windows.window_row(window)["error_message"].startswith(
        "budget_window_stalled: "
    )


def test_a_window_with_a_year_under_way_is_not_idle(store, windows, window) -> None:
    windows.set_years(window, [2026], status="running", modal_call_id="fc-running")

    listed = store.list_idle_windows(idle_since_before=IDLE_CUTOFF)
    settled = store.settle_idle_window(
        window.window_run_id, idle_since_before=IDLE_CUTOFF, now=LATER
    )

    assert window.window_run_id not in listed
    assert settled == WindowAdvance(status=RunStatus.PENDING, changed=False)
