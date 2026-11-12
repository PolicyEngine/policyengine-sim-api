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
from policyengine_simulation_contract.stage12_execution import (
    ReportRunIds,
    year_calculation,
)
from policyengine_simulation_executor.stage12_runtime import (
    budget_window_scheduling,
)
from policyengine_simulation_executor.stage12_runtime.budget_window_scheduling import (
    ModalYearStarter,
    advance_window_of_year,
    coordinate_window_year,
    start_window_of_year,
)
from policyengine_stage12_persistence import RunStatus, WindowAdvance, YearToStart
from stage12_fake_report_runs import report_run_context
from stage12_runtime_support import report_execution_input

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


def test_the_year_starter_starts_this_application_s_coordinator_for_the_year(
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
    report, context = report_execution_input(), report_run_context()
    next_year_runs = ReportRunIds(
        report_run_id=uuid4(),
        baseline_simulation_run_id=uuid4(),
        reform_simulation_run_id=uuid4(),
    )
    starter = ModalYearStarter(
        payload=report.model_dump(mode="json"),
        context=context.model_dump(mode="json"),
        observability_context={"traceparent": "trace"},
        clock=lambda: NOW,
    )

    assert starter(YearToStart(year=2029, run_ids=next_year_runs)) == "fc-next-year"
    expected_report, expected_context = year_calculation(
        report, context, year=2029, run_ids=next_year_runs, created_at=NOW
    )
    assert calls == [
        (
            "from_name",
            (context.modal_application, "coordinate_report"),
            {"environment_name": context.modal_environment},
        ),
        (
            "spawn",
            (
                expected_report.model_dump(mode="json"),
                expected_context.model_dump(mode="json"),
            ),
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


def test_a_malformed_input_fails_only_the_start() -> None:
    # The coordinator refuses the same input; the start raises, so the year
    # fails as refused instead of the starter failing to be built.
    starter = ModalYearStarter(payload={"report": 1}, context={"context": 1})

    with pytest.raises(ValueError, match="validation error"):
        starter(
            YearToStart(
                year=2029,
                run_ids=ReportRunIds(
                    report_run_id=uuid4(),
                    baseline_simulation_run_id=uuid4(),
                    reform_simulation_run_id=uuid4(),
                ),
            )
        )


class RecordingWindows(FakeWindowStore):
    """Records the order of the window steps and the coordinator call."""

    def __init__(self, events, *, window_run_id=None):
        super().__init__()
        self.events = events
        self.window_run_id = window_run_id or WINDOW_RUN_ID

    def start_window(self, report_run_id, *, started_at):
        self.events.append(("start", report_run_id))
        return True

    def window_of_year(self, report_run_id):
        self.events.append(("window_of_year", report_run_id))
        return self.window_run_id

    def advance_window(self, window_run_id, *, now, start_year=None):
        self.events.append(("advance", window_run_id, start_year))
        return WindowAdvance(status=RunStatus.RUNNING, changed=False)


WINDOW_RUN_ID = uuid4()


def _year_call(events, coordinate, *, windows=None, context=None):
    context = context or report_run_context().model_dump(mode="json")
    return coordinate_window_year(
        coordinate,
        payload=report_execution_input().model_dump(mode="json"),
        context=context,
        windows=(lambda: windows or RecordingWindows(events)),
        observability_context=lambda: {"traceparent": "trace"},
    )


def test_a_year_marks_its_window_running_then_advances_it_when_it_ends() -> None:
    events = []

    def coordinate():
        events.append(("coordinate",))
        return {"status": "succeeded"}

    assert _year_call(events, coordinate) == {"status": "succeeded"}

    report_run_id = report_run_context().report_run.report_run_id
    # The window is looked up once, before the coordinator runs.
    assert [event[0] for event in events] == [
        "window_of_year",
        "start",
        "coordinate",
        "advance",
    ]
    assert events[1] == ("start", report_run_id)
    assert events[3][1] == WINDOW_RUN_ID
    starter = events[3][2]
    assert isinstance(starter, ModalYearStarter)
    assert starter.payload == report_execution_input().model_dump(mode="json")
    assert starter.context == report_run_context().model_dump(mode="json")
    assert starter.observability_context == {"traceparent": "trace"}


@pytest.mark.parametrize(
    "ending",
    [RuntimeError("Stage 12 report coordination failed"), BaseException("cancelled")],
    ids=["failure", "cancellation"],
)
def test_a_failed_or_cancelled_year_still_advances_its_window(ending) -> None:
    events = []

    def coordinate():
        raise ending

    with pytest.raises(type(ending)):
        _year_call(events, coordinate)

    assert [event[0] for event in events] == ["window_of_year", "start", "advance"]


def test_a_preempted_year_leaves_its_window_to_the_restarted_call() -> None:
    events = []

    def coordinate():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _year_call(events, coordinate)

    assert [event[0] for event in events] == ["window_of_year", "start"]


def test_a_call_naming_no_report_run_touches_no_window() -> None:
    events = []
    comparison_context = (
        report_run_context()
        .model_copy(update={"report_run": None})
        .model_dump(mode="json")
    )

    def windows():
        message = "a comparison call has no window"
        raise AssertionError(message)

    result = coordinate_window_year(
        lambda: {"status": "succeeded"},
        payload={},
        context=comparison_context,
        windows=windows,
        observability_context=lambda: None,
    )

    assert result == {"status": "succeeded"}
    assert events == []


def test_a_window_store_that_cannot_be_built_leaves_the_year_to_run(caplog) -> None:
    def windows():
        msg = "MODAL_WORKER_DATABASE_URL is not a complete postgresql:// URL"
        raise ValueError(msg)

    with caplog.at_level(logging.WARNING, logger=budget_window_scheduling.__name__):
        result = coordinate_window_year(
            lambda: {"status": "succeeded"},
            payload=report_execution_input().model_dump(mode="json"),
            context=report_run_context().model_dump(mode="json"),
            windows=windows,
            observability_context=lambda: None,
        )

    assert result == {"status": "succeeded"}
    assert "ValueError" in caplog.text
    assert "postgresql://" not in caplog.text


def test_the_v2_coordinator_runs_each_call_as_a_window_year(monkeypatch) -> None:
    from policyengine_simulation_executor import stage12_runtime
    from test_stage12_modal_app import RecordingRuntime, _load

    module = _load(monkeypatch)
    monkeypatch.setattr(
        module.modal, "current_function_call_id", lambda: "fc-1", raising=False
    )
    coordinator_trace = {"traceparent": "00-coordinator-trace-01"}

    class TracedRuntime(RecordingRuntime):
        def capture_context(self) -> dict:
            return coordinator_trace

    monkeypatch.setattr(
        module, "init_process_observability", lambda **_: TracedRuntime()
    )
    received = []

    def window_year(coordinate, *, payload, context, windows, observability_context):
        # The next years' calls carry the coordinator's trace, captured
        # only when they are started.
        received.append((payload, context, observability_context()))
        return coordinate()

    monkeypatch.setattr(stage12_runtime, "coordinate_window_year", window_year)
    monkeypatch.setattr(
        stage12_runtime,
        "coordinate_report",
        lambda payload, context, parent, **_kwargs: {"status": "succeeded"},
    )

    result = module.coordinate_report(
        {"report": 1}, {"context": 1}, {"parent": 1}, observability_context=None
    )

    assert result == {"status": "succeeded"}
    assert received == [({"report": 1}, {"context": 1}, coordinator_trace)]


class NoWindow(RecordingWindows):
    """A report run that is no budget window's year."""

    def window_of_year(self, report_run_id):
        self.events.append(("window_of_year", report_run_id))


def test_a_report_run_outside_any_window_makes_no_window_writes() -> None:
    events = []

    result = _year_call(
        events, lambda: {"status": "succeeded"}, windows=NoWindow(events)
    )

    assert result == {"status": "succeeded"}
    assert [event[0] for event in events] == ["window_of_year"]


class UnreadableWindow(RecordingWindows):
    """A window lookup that fails before the coordinator runs."""

    def __init__(self, events):
        super().__init__(events)
        self.lookups = 0

    def window_of_year(self, report_run_id):
        self.lookups += 1
        if self.lookups == 1:
            message = "database unavailable"
            raise RuntimeError(message)
        return super().window_of_year(report_run_id)


def test_a_failed_lookup_still_advances_the_window_when_the_year_ends() -> None:
    events = []

    _year_call(
        events, lambda: {"status": "succeeded"}, windows=UnreadableWindow(events)
    )

    # Nothing is marked before the coordinator runs; the window is looked up
    # again, and advanced, once the year has ended.
    assert [event[0] for event in events] == ["window_of_year", "advance"]


def test_an_unreadable_observability_context_still_advances_the_window() -> None:
    events = []

    def observability_context():
        message = "no trace"
        raise RuntimeError(message)

    result = coordinate_window_year(
        lambda: {"status": "succeeded"},
        payload=report_execution_input().model_dump(mode="json"),
        context=report_run_context().model_dump(mode="json"),
        windows=lambda: RecordingWindows(events),
        observability_context=observability_context,
    )

    assert result == {"status": "succeeded"}
    starter = events[-1][2]
    assert events[-1][0] == "advance"
    assert starter.observability_context is None
