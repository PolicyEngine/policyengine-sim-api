"""The scheduled restart check's telemetry, and the service it runs."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from policyengine_simulation_entry.run_restart_check import (
    RESTART_STATEMENT_TIMEOUT_MILLISECONDS,
    ObservabilityRestartTelemetry,
    run_scheduled_check,
    scheduled_restart_service,
)
from policyengine_simulation_entry.run_restarts import (
    RestartOutcome,
    RestartSettings,
    RestartSummary,
    RunRestartService,
)
from policyengine_simulation_persistence import RestartKind, UnstartedRun
from policyengine_stage12_persistence import BudgetWindowStore

if TYPE_CHECKING:
    from collections.abc import Iterator

RUN = UnstartedRun(
    kind=RestartKind.REPORT_RUN,
    run_id=UUID("00000000-0000-4000-8000-0000000000e1"),
    created_at=datetime(2026, 11, 13, 9, tzinfo=UTC),
)
DATABASE_URL = "postgresql://worker:secret@database.example/postgres"


@dataclass
class RecordingRuntime:
    """Records what the telemetry asks of the observability runtime."""

    operations: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    contexts: list[dict[str, Any]] = field(default_factory=list)
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    open_operations: list[str] = field(default_factory=list)

    @contextmanager
    def operation(
        self, name: str, *, attributes: dict[str, Any] | None = None
    ) -> Iterator[None]:
        self.operations.append((name, dict(attributes or {})))
        self.open_operations.append(name)
        try:
            yield
        finally:
            self.open_operations.pop()

    def set_context(self, **attributes: Any) -> None:
        self.contexts.append({"within": list(self.open_operations), **attributes})

    def event(
        self,
        name: str,
        *,
        severity: str = "INFO",
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.events.append((name, severity, dict(attributes or {})))

    def capture_context(self) -> dict[str, str]:
        return {"traceparent": "00-restart-check"}


def test_each_restart_is_an_operation_named_by_its_registered_stage() -> None:
    runtime = RecordingRuntime()
    telemetry = ObservabilityRestartTelemetry(runtime)

    with telemetry.restart_scope(RUN, age_seconds=210.5):
        within = list(runtime.open_operations)

    assert within == ["v2_run_restart"]
    assert runtime.operations == [
        (
            "v2_run_restart",
            {
                "run_kind": "report_run",
                "run_id": str(RUN.run_id),
                "run_age_seconds": 210.5,
            },
        )
    ]


@pytest.mark.parametrize(
    ("outcome", "error_type", "severity"),
    [
        (RestartOutcome.RESTARTED, None, "INFO"),
        (RestartOutcome.STARTED_MEANWHILE, None, "INFO"),
        (RestartOutcome.LEFT, None, "INFO"),
        (RestartOutcome.DEFERRED, "OperationalError", "WARNING"),
        (RestartOutcome.CALL_NOT_RECORDED, "OperationalError", "WARNING"),
        (RestartOutcome.FAILED, "ConnectionError", "WARNING"),
    ],
)
def test_an_outcome_is_set_on_the_restart_and_logged_as_an_event(
    outcome, error_type, severity
) -> None:
    runtime = RecordingRuntime()
    telemetry = ObservabilityRestartTelemetry(runtime)

    with telemetry.restart_scope(RUN, age_seconds=200.0):
        telemetry.record_outcome(RUN, outcome, error_type=error_type)

    expected = {"restart_outcome": outcome.value}
    if error_type is not None:
        expected["restart_error_type"] = error_type
    assert runtime.contexts == [{"within": ["v2_run_restart"], **expected}]
    assert runtime.events == [
        (
            "v2_run_restart_outcome",
            severity,
            {"run_kind": "report_run", "run_id": str(RUN.run_id), **expected},
        )
    ]


def test_a_restarted_call_continues_the_checks_trace() -> None:
    telemetry = ObservabilityRestartTelemetry(RecordingRuntime())

    assert telemetry.capture_context() == {"traceparent": "00-restart-check"}


@dataclass
class FakeRestartService:
    """Answers one check with a summary, noting the operation it ran in."""

    runtime: RecordingRuntime
    summary: RestartSummary
    ran_within: list[list[str]] = field(default_factory=list)

    async def restart_unstarted_runs(self) -> RestartSummary:
        self.ran_within.append(list(self.runtime.open_operations))
        return self.summary


def test_a_check_runs_in_its_own_operation_and_answers_its_counts() -> None:
    runtime = RecordingRuntime()
    summary = RestartSummary(listed=3, restarted=1, failed=1, left=1)
    restart_service = FakeRestartService(runtime, summary)

    counts = run_scheduled_check(restart_service, runtime)

    assert counts == {
        "listed": 3,
        "restarted": 1,
        "call_not_recorded": 0,
        "started_meanwhile": 0,
        "left": 1,
        "deferred": 0,
        "failed": 1,
    }
    assert restart_service.ran_within == [["v2_run_restart_check"]]
    assert runtime.operations == [("v2_run_restart_check", {})]
    assert runtime.contexts == [
        {"within": ["v2_run_restart_check"]}
        | {f"restart_{name}": count for name, count in counts.items()}
    ]


def test_the_scheduled_service_restarts_in_its_modal_environments_deployment() -> None:
    scheduled = scheduled_restart_service(
        database_url=DATABASE_URL,
        modal_environment="main",
        runtime=RecordingRuntime(),
    )
    try:
        assert isinstance(scheduled.service, RunRestartService)
        assert scheduled.settings == RestartSettings(
            environment="production", modal_environment="main"
        )
    finally:
        scheduled.engine.dispose()


def test_the_scheduled_window_advance_bounds_each_statement_as_the_check_does() -> None:
    """A window's lock or reads can't hold the check past its Modal timeout."""
    scheduled = scheduled_restart_service(
        database_url=DATABASE_URL,
        modal_environment="main",
        runtime=RecordingRuntime(),
    )
    try:
        windows = scheduled.service._stores.windows
        assert isinstance(windows, BudgetWindowStore)
        assert windows.engine is scheduled.engine
        assert (
            windows.statement_timeout_milliseconds
            == RESTART_STATEMENT_TIMEOUT_MILLISECONDS
        )
    finally:
        scheduled.engine.dispose()


def test_the_scheduled_service_refuses_a_modal_environment_no_deployment_uses() -> None:
    with pytest.raises(ValueError, match="local"):
        scheduled_restart_service(
            database_url=DATABASE_URL,
            modal_environment="local",
            runtime=RecordingRuntime(),
        )


def test_the_restart_module_imports_without_the_web_service(tmp_path) -> None:
    """The restart check runs in the sweep's image, which has no web packages.

    Importing it must not import the FastAPI app, whose authentication needs
    PyJWT and cryptography, which that image does not install.
    """
    import subprocess  # this test alone starts a process
    import sys  # this test alone starts a process

    script = tmp_path / "import_restart_check.py"
    script.write_text(
        "import sys\n"
        "class Refuse:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in {'jwt', 'cryptography', 'uvicorn'}:\n"
        "            raise ImportError(name)\n"
        "sys.meta_path.insert(0, Refuse())\n"
        "import policyengine_simulation_entry.run_restart_check\n"
        "assert 'policyengine_simulation_entry.app' not in sys.modules\n",
        encoding="utf-8",
    )

    completed = subprocess.run(  # the test's own interpreter and script
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
