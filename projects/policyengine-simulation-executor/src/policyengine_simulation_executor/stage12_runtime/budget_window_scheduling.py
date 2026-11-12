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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from policyengine_simulation_contract.stage12_execution import (
    ReportExecutionInput,
    Stage12InvocationContext,
    year_calculation,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from policyengine_stage12_persistence import (
        WindowAdvance,
        YearStarter,
        YearToStart,
    )

logger = logging.getLogger(__name__)

# The v2 application's coordinator function.
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
    coordinator is this application's ``coordinate_report``. Its input is
    derived from this coordinator's own ``payload`` and ``context``: only the
    year, its runs, its artifact prefix and its call's creation time differ.
    They are checked only when a year is started, so a malformed input fails
    that start, as Modal refusing the call would.
    """

    payload: Mapping[str, Any]
    context: Mapping[str, Any]
    observability_context: dict[str, Any] | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __call__(self, year_to_start: YearToStart, /) -> str:
        """Spawn the year's coordinator call and return its Modal call ID.

        Raises ValueError when the coordinator's own input is malformed.
        """
        context = Stage12InvocationContext.model_validate(self.context)
        year_report, year_context = year_calculation(
            ReportExecutionInput.model_validate(self.payload),
            context,
            year=year_to_start.year,
            run_ids=year_to_start.run_ids,
            created_at=self.clock(),
        )
        modal: Any = import_module("modal")
        function = modal.Function.from_name(
            context.modal_application,
            COORDINATOR_FUNCTION_NAME,
            environment_name=context.modal_environment,
        )
        call = function.spawn(
            year_report.model_dump(mode="json"),
            year_context.model_dump(mode="json"),
            observability_context=self.observability_context,
        )
        return call.object_id


def coordinate_window_year[CoordinatorResult](
    coordinate: Callable[[], CoordinatorResult],
    *,
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
    windows: Callable[[], YearWindowWriter],
    observability_context: Callable[[], dict[str, Any] | None],
) -> CoordinatorResult:
    """Run one coordinator call, starting and advancing its year's window.

    A call naming a report run marks the run's window running before
    ``coordinate`` runs, and advances it once ``coordinate`` returns, raises
    or is cancelled, starting waiting years in this application. A
    pre-emption advances nothing: Modal runs the call again, and the
    restarted call advances when it ends, even when it finds its year
    already finished. Advancing more than once is safe, because an advance
    starts only waiting years and never more than five under way. A call
    naming no report run, or a store that cannot be built, touches no
    window.
    """
    report_run_id = _report_run_named_by(context)
    window_store = None if report_run_id is None else _built(windows)
    if report_run_id is None or window_store is None:
        return coordinate()
    start_window_of_year(window_store, report_run_id)
    try:
        result = coordinate()
    except KeyboardInterrupt:
        raise
    except BaseException:
        _advance_after_year(
            window_store, report_run_id, payload, context, observability_context
        )
        raise
    _advance_after_year(
        window_store, report_run_id, payload, context, observability_context
    )
    return result


def _advance_after_year(
    windows: YearWindowWriter,
    report_run_id: UUID,
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
    observability_context: Callable[[], dict[str, Any] | None],
) -> None:
    """Advance the window of a year whose coordinator call has ended."""
    advance_window_of_year(
        windows,
        report_run_id,
        start_year=ModalYearStarter(
            payload=payload,
            context=context,
            observability_context=observability_context(),
        ),
    )


def _report_run_named_by(context: Mapping[str, Any]) -> UUID | None:
    """Return the report run a coordinator call's context names, if any."""
    report_run = context.get("report_run")
    if not isinstance(report_run, Mapping):
        return None
    try:
        return UUID(str(report_run["report_run_id"]))
    except (KeyError, ValueError):
        # The coordinator refuses the malformed context itself.
        return None


def _built(windows: Callable[[], YearWindowWriter]) -> YearWindowWriter | None:
    """Build the window store, or log why it can't be built and return None."""
    try:
        return windows()
    except Exception as error:  # noqa: BLE001  # the year runs whether or not its window can be recorded
        logger.warning(
            "Could not open the budget-window store (%s)", type(error).__name__
        )
        return None


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
