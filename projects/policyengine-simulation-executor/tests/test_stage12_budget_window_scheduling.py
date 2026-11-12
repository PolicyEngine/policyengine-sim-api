"""A budget window's years, started and finished by their coordinators.

When a year's coordinator claims its report run, the window is marked
running; when the year ends, the coordinator advances the window, which
starts the earliest waiting years in this coordinator's own Modal
application, or finishes the window. Neither step ever changes the year's
own outcome: a failure is logged.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from policyengine_simulation_executor.stage12_runtime import (
    budget_window_scheduling,
)
from policyengine_simulation_executor.stage12_runtime.budget_window_scheduling import (
    ModalYearStarter,
    advance_window_of_year,
    start_window_of_year,
)
from policyengine_stage12_persistence import RunStatus, WindowAdvance

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)


class FakeWindowStore:
    """Holds which window each year belongs to and records each write."""

    def __init__(self, windows_of_years=None, *, failing=None):
        self.windows_of_years = windows_of_years or {}
        self.failing = failing
        self.started = []
        self.advanced = []

    def window_of_year(self, report_run_id):
        if self.failing is not None:
            raise self.failing
        return self.windows_of_years.get(report_run_id)

    def start_window(self, report_run_id, *, started_at):
        if self.failing is not None:
            raise self.failing
        self.started.append((report_run_id, started_at))
        return True

    def advance_window(self, window_run_id, *, now, start_year=None):
        self.advanced.append((window_run_id, now, start_year))
        return WindowAdvance(
            status=RunStatus.RUNNING, changed=False, started_years=(2031,)
        )


def test_the_year_starter_starts_the_coordinator_by_report_run_id(
    monkeypatch,
) -> None:
    calls = []

    def from_name(*args, **kwargs):
        calls.append(("from_name", args, kwargs))

        def spawn(*spawn_args, **spawn_kwargs):
            calls.append(("spawn", spawn_args, spawn_kwargs))
            return SimpleNamespace(object_id="fc-next-year")

        return SimpleNamespace(spawn=spawn)

    fake_modal = SimpleNamespace(Function=SimpleNamespace(from_name=from_name))

    def import_modal(module_name):
        assert module_name == "modal"
        return fake_modal

    monkeypatch.setattr(budget_window_scheduling, "import_module", import_modal)
    report_run_id = uuid4()
    starter = ModalYearStarter(
        application_name="policyengine-simulation-v2-6-17-1",
        environment="staging",
        observability_context={"traceparent": "trace"},
    )

    assert starter(report_run_id) == "fc-next-year"
    assert calls == [
        (
            "from_name",
            ("policyengine-simulation-v2-6-17-1", "coordinate_report"),
            {"environment_name": "staging"},
        ),
        (
            "spawn",
            (str(report_run_id),),
            {"observability_context": {"traceparent": "trace"}},
        ),
    ]


def test_an_ended_year_advances_its_window_with_the_starter() -> None:
    year_run_id, window_run_id = uuid4(), uuid4()
    windows = FakeWindowStore({year_run_id: window_run_id})

    def start_year(report_run_id):
        return f"fc-{report_run_id}"

    advance = advance_window_of_year(
        windows, year_run_id, start_year=start_year, now=NOW
    )

    assert windows.advanced == [(window_run_id, NOW, start_year)]
    assert advance == WindowAdvance(
        status=RunStatus.RUNNING, changed=False, started_years=(2031,)
    )


def test_a_report_run_outside_any_window_advances_nothing() -> None:
    windows = FakeWindowStore()

    assert (
        advance_window_of_year(
            windows, uuid4(), start_year=lambda report_run_id: "fc", now=NOW
        )
        is None
    )
    assert windows.advanced == []


def test_a_failed_advance_is_logged_and_never_raised(caplog) -> None:
    windows = FakeWindowStore(failing=RuntimeError("database unavailable"))
    year_run_id = uuid4()

    with caplog.at_level(logging.WARNING, logger=budget_window_scheduling.__name__):
        advance = advance_window_of_year(
            windows, year_run_id, start_year=lambda report_run_id: "fc", now=NOW
        )

    assert advance is None
    assert str(year_run_id) in caplog.text
    assert "RuntimeError" in caplog.text
    # The error's own text may hold connection details; it is never logged.
    assert "database unavailable" not in caplog.text


def test_a_claimed_year_marks_its_window_running() -> None:
    year_run_id = uuid4()
    windows = FakeWindowStore()

    start_window_of_year(windows, year_run_id, now=NOW)

    assert windows.started == [(year_run_id, NOW)]


def test_a_failed_window_start_is_logged_and_never_raised(caplog) -> None:
    windows = FakeWindowStore(failing=RuntimeError("database unavailable"))

    with caplog.at_level(logging.WARNING, logger=budget_window_scheduling.__name__):
        start_window_of_year(windows, uuid4(), now=NOW)

    assert "RuntimeError" in caplog.text
    assert "database unavailable" not in caplog.text


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_an_interruption_while_advancing_propagates(interruption) -> None:
    # Modal pre-empts or cancels the call: the restarted call advances again.
    windows = FakeWindowStore(failing=interruption())

    with pytest.raises(interruption):
        advance_window_of_year(
            windows, uuid4(), start_year=lambda report_run_id: "fc", now=NOW
        )
