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
  writes as a report.

The job runs only the image of the revision that serves traffic: the deploy
updates it when that revision is promoted, and a rollback moves it back, so
a rejected candidate never records anything. Run it with
``python -m policyengine_simulation_entry.safety_check``; it reads
``SIMULATION_DATABASE_URL`` and sim's Modal token (``MODAL_TOKEN_ID``,
``MODAL_TOKEN_SECRET``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import asdict
from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_entry.modal_calls import OWN_PROCESS_SIGNALS
from policyengine_simulation_entry.simulation_database import SimulationDatabase

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from policyengine_simulation_entry.run_recording import RecordingPass

logger = logging.getLogger(__name__)

EXIT_PASSED = 0
EXIT_PASS_FAILED = 1
EXIT_NOT_CONFIGURED = 2


class DueOutcomeRecorder(Protocol):
    """Run one recording pass (``RunRecorder``)."""

    async def record_due_outcomes(self) -> RecordingPass:
        """Fail abandoned dispatches, then check every due call."""
        ...


def _database_recorder(database_url: str) -> DueOutcomeRecorder:
    return SimulationDatabase.from_url(database_url).call_recorder()


def main(
    *,
    environment: Mapping[str, str] | None = None,
    build_recorder: Callable[[str], DueOutcomeRecorder] = _database_recorder,
) -> int:
    """Run one pass and return the process exit status.

    Only a signal to this process propagates; any other error fails the
    execution, and the next scheduled execution tries again.
    """
    values = os.environ if environment is None else environment
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
    logger.info("safety_check_pass", extra=asdict(recording))
    return EXIT_PASSED


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
