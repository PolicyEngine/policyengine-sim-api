"""The safety check: one recording pass, run as a Cloud Run job, then exit.

A Modal call reports when it ends, and sim records its outcome then
(``call_report_route``). The safety check covers a call whose report never
arrived: Cloud Scheduler starts this job every ``SAFETY_CHECK_INTERVAL``, and
each execution runs one ``RunRecorder`` pass, which

- fails runs and comparison steps left ``dispatching`` for ten minutes (sim
  stopped between claiming them and recording their Modal call), so they can
  be submitted again, and pending comparison steps whose run failed;
- asks Modal about running calls first dispatched, or last checked, at least
  one interval ago, and records what it reports, with the same conditional
  writes as a report;
- starts the economy work left waiting: comparison steps whose runs both
  succeeded, and runs a submitter left pending (``SafetyCheckPass``).

The job runs only the image of the revision that serves traffic: the deploy
updates it when that revision is promoted, and a rollback moves it back, so
a rejected candidate never records anything. Run it with
``python -m policyengine_simulation_entry.safety_check``; it reads
``SIMULATION_DATABASE_URL``, sim's Modal token (``MODAL_TOKEN_ID``,
``MODAL_TOKEN_SECRET``), and the Modal environment and deployment it starts
economy calls in (``SIMULATION_CALL_REPORT_MODAL_ENVIRONMENT``,
``APP_ENVIRONMENT``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_observability.observability import (
    init_process_observability,
)
from policyengine_simulation_persistence.run_store import utc_now

from policyengine_simulation_entry.economy_reports import ResumedWork
from policyengine_simulation_entry.modal_calls import OWN_PROCESS_SIGNALS
from policyengine_simulation_entry.simulation_database import SimulationDatabase

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from policyengine_simulation_entry.run_recording import RecordingPass

__all__ = ["ResumedWork", "SafetyCheckPass", "main"]

logger = logging.getLogger(__name__)

EXIT_PASSED = 0
EXIT_PASS_FAILED = 1
EXIT_NOT_CONFIGURED = 2


class Telemetry(Protocol):
    """The observability runtime's part the job uses."""

    def shutdown(self) -> None:
        """Flush and stop telemetry."""
        ...


def _start_observability(environment: str) -> Telemetry:
    return init_process_observability(
        service_name="policyengine-simulation-safety-check",
        service_role="simulation_safety_check",
        platform="google_cloud_run",
        environment=environment,
    )


class DueOutcomeRecorder(Protocol):
    """Run one recording pass (``RunRecorder``)."""

    async def record_due_outcomes(self) -> RecordingPass:
        """Fail abandoned dispatches, then check every due call."""
        ...


class WaitingWork(Protocol):
    """Start the economy work left waiting (``EconomyReports``)."""

    async def resume_waiting_work(self, *, now: datetime) -> ResumedWork:
        """Start ready comparisons and runs left pending."""
        ...


@dataclass(frozen=True)
class SafetyCheckPass:
    """One pass: record the due calls, then start the work left waiting."""

    recorder: DueOutcomeRecorder
    waiting: WaitingWork | None
    clock: Callable[[], datetime] = utc_now

    async def record_due_outcomes(self) -> RecordingPass:
        """Record due calls' outcomes, then start waiting comparisons and runs."""
        recording = await self.recorder.record_due_outcomes()
        if self.waiting is not None:
            resumed = await self.waiting.resume_waiting_work(now=self.clock())
            recording.comparisons_started = resumed.comparisons_started
            recording.runs_started = resumed.runs_started
        return recording


def _database_recorder(database_url: str) -> DueOutcomeRecorder:
    """Build the pass; it starts waiting work when the Modal environment is set."""
    database = SimulationDatabase.from_url(database_url)
    modal_environment = os.environ.get("SIMULATION_CALL_REPORT_MODAL_ENVIRONMENT", "")
    waiting = (
        database.economy_routes(
            artifact_environment=os.environ.get("APP_ENVIRONMENT", "local").lower(),
            modal_environment=modal_environment,
        ).reports
        if modal_environment
        else None
    )
    return SafetyCheckPass(database.call_recorder(), waiting)


def main(
    *,
    environment: Mapping[str, str] | None = None,
    build_recorder: Callable[[str], DueOutcomeRecorder] = _database_recorder,
    start_observability: Callable[[str], Telemetry] = _start_observability,
) -> int:
    """Run one pass and return the process exit status.

    Logs go through the observability runtime, as the web service's do. Only
    a signal to this process propagates; any other error fails the
    execution, and the next scheduled execution tries again.
    """
    values = os.environ if environment is None else environment
    telemetry = start_observability(values.get("APP_ENVIRONMENT", "local"))
    try:
        return _run_pass(values, build_recorder)
    finally:
        telemetry.shutdown()


def _run_pass(
    values: Mapping[str, str], build_recorder: Callable[[str], DueOutcomeRecorder]
) -> int:
    database_url = values.get("SIMULATION_DATABASE_URL", "")
    if not database_url:
        logger.error("safety_check_not_configured")
        print("SIMULATION_DATABASE_URL is not set.", file=sys.stderr)  # noqa: T201  # the job's own error output
        return EXIT_NOT_CONFIGURED
    try:
        recording = asyncio.run(build_recorder(database_url).record_due_outcomes())
    except BaseException as error:
        if isinstance(error, OWN_PROCESS_SIGNALS):
            raise
        logger.exception(
            "safety_check_pass_failed", extra={"error_type": type(error).__name__}
        )
        return EXIT_PASS_FAILED
    # The observability log handler keeps a record's policyengine_attributes.
    logger.info(
        "safety_check_pass", extra={"policyengine_attributes": asdict(recording)}
    )
    return EXIT_PASSED


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    sys.exit(main())
