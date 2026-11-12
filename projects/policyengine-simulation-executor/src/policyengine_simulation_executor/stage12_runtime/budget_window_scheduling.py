"""A budget window's years, started and finished by their coordinators.

A budget window's years are report runs naming its budget-window run. The
simulation service creates every year and starts the first ones. A year's
coordinator marks the window running when it claims its year and, once the
year has ended, advances the window: it starts the earliest waiting years
in its own Modal application, so that at most five are under way, or marks
the window failed or succeeded (see ``BudgetWindowStore``).

Neither step changes the year's own outcome. A failed write is logged,
without the error's text, and the stuck-run sweep finishes a window that
no year advances any more. A pre-emption or cancellation propagates, as
everywhere in the coordinator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from uuid import UUID

    from policyengine_stage12_persistence import WindowAdvance, YearStarter

logger = logging.getLogger(__name__)

# The v2 application's coordinator function, which takes a report run's ID.
COORDINATOR_FUNCTION_NAME = "coordinate_report"


class YearWindowWriter(Protocol):
    """A coordinator's budget-window writes; ``BudgetWindowStore`` implements them."""

    def window_of_year(self, report_run_id: UUID) -> UUID | None:
        """Return the budget-window run a report run is a year of, if any."""
        ...

    def start_window(self, report_run_id: UUID, *, started_at: datetime) -> bool:
        """Mark the window of a claimed year running; return whether it changed."""
        ...

    def advance_window(
        self,
        window_run_id: UUID,
        *,
        now: datetime,
        start_year: YearStarter | None = None,
    ) -> WindowAdvance:
        """Finish the window from its years, else start its waiting years."""
        ...


@dataclass(frozen=True)
class ModalYearStarter:
    """Starts a year's coordinator call in this coordinator's Modal application.

    Every year of a window runs on the same .py version, so the next year's
    coordinator is this application's ``coordinate_report``, called with the
    year's report run ID alone.
    """

    application_name: str
    environment: str | None
    observability_context: dict[str, Any] | None = None

    def __call__(self, report_run_id: UUID, /) -> str:
        """Spawn the year's coordinator call and return its Modal call ID."""
        modal: Any = import_module("modal")
        function = modal.Function.from_name(
            self.application_name,
            COORDINATOR_FUNCTION_NAME,
            environment_name=self.environment,
        )
        call = function.spawn(
            str(report_run_id),
            observability_context=self.observability_context,
        )
        return call.object_id


def start_window_of_year(
    windows: YearWindowWriter,
    report_run_id: UUID,
    *,
    now: datetime | None = None,
) -> None:
    """Mark the window of a year this call just claimed running; never raise.

    A report run that is no window's year changes nothing.
    """
    try:
        windows.start_window(report_run_id, started_at=now or datetime.now(UTC))
    except Exception as error:  # noqa: BLE001  # the year runs whether or not its window records the start
        _log_failed_write("mark the budget window of", report_run_id, error)


def advance_window_of_year(
    windows: YearWindowWriter,
    report_run_id: UUID,
    *,
    start_year: YearStarter,
    now: datetime | None = None,
) -> WindowAdvance | None:
    """Advance the window of a year that has ended; never raise.

    Returns what the advance did, or None when the report run is no window's
    year or the advance failed.
    """
    try:
        window_run_id = windows.window_of_year(report_run_id)
        if window_run_id is None:
            return None
        advance = windows.advance_window(
            window_run_id, now=now or datetime.now(UTC), start_year=start_year
        )
    except Exception as error:  # noqa: BLE001  # the year's outcome stands whether or not its window advances
        _log_failed_write("advance the budget window of", report_run_id, error)
        return None
    if advance.started_years or advance.changed:
        logger.info(
            "Budget-window run %s is %s after report run %s; started years %s",
            window_run_id,
            advance.status.value,
            report_run_id,
            list(advance.started_years),
        )
    return advance


def _log_failed_write(action: str, report_run_id: UUID, error: Exception) -> None:
    # The error's text may hold connection details, so only its type is logged.
    logger.warning(
        "Could not %s report run %s (%s)",
        action,
        report_run_id,
        type(error).__name__,
    )
