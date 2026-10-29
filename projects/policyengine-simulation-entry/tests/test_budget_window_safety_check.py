"""The safety check also starts the years of budget windows left waiting."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from policyengine_simulation_entry import safety_check
from policyengine_simulation_entry.budget_window_reports import BudgetWindowReports
from policyengine_simulation_entry.run_recording import RecordingPass

DATABASE_URL = "postgresql://runtime:secret@db.example/postgres"
NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)


class _OnePass:
    async def record_due_outcomes(self) -> RecordingPass:
        return RecordingPass(checked=1)


class _WaitingWindows:
    def __init__(self) -> None:
        self.resumed_at: list[datetime] = []

    async def resume_waiting_work(self, *, now: datetime) -> int:
        self.resumed_at.append(now)
        return 3


def test_a_pass_starts_the_years_of_budget_windows_left_waiting() -> None:
    windows = _WaitingWindows()
    recorder = safety_check.SafetyCheckPass(
        _OnePass(), None, budget_windows=windows, clock=lambda: NOW
    )

    recording = asyncio.run(recorder.record_due_outcomes())

    assert windows.resumed_at == [NOW]
    assert recording.budget_window_years_started == 3
    assert recording.checked == 1


def test_a_pass_without_budget_windows_starts_no_years() -> None:
    recording = asyncio.run(
        safety_check.SafetyCheckPass(_OnePass(), None).record_due_outcomes()
    )

    assert recording.budget_window_years_started == 0


def test_the_job_resumes_budget_windows_in_its_own_modal_environment() -> None:
    environment = {
        "SIMULATION_CALL_REPORT_MODAL_ENVIRONMENT": "staging",
        "APP_ENVIRONMENT": "Staging",
    }

    configured = safety_check._database_recorder(DATABASE_URL, environment=environment)
    unconfigured = safety_check._database_recorder(DATABASE_URL, environment={})

    assert isinstance(configured.budget_windows, BudgetWindowReports)
    assert configured.budget_windows.economy is configured.waiting
    assert unconfigured.budget_windows is None
