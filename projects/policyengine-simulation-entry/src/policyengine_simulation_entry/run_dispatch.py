"""Start each simulation run's, or comparison step's, Modal call exactly once.

Starting a call takes three conditional writes around Modal's ``spawn``:

1. claim: move the runs from ``pending`` to ``dispatching``. Only the instance
   whose claim changes them goes on; every other caller stops here.
2. spawn the Modal call and wait only for Modal's acknowledgement.
3. record the call ID, moving the runs to ``running`` with their first check
   due shortly; or, when Modal refused the call, fail them.

If the instance stops between 1 and 3, the runs stay ``dispatching`` until the
recording loop fails them as abandoned. No route starts calls yet.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_persistence import RunError, RunErrorCode
from policyengine_simulation_persistence.run_store import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable, Collection
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_entry.modal_calls import CallDispatcher, ModalRequest

logger = logging.getLogger(__name__)


def _event_name(prefix: str, outcome: str) -> str:
    """Name a log event for a simulation run or a comparison step."""
    return f"{prefix}_{outcome}"


# The first check of a new call; the recording loop backs off from there.
FIRST_CHECK_DELAY = timedelta(seconds=2)


class DispatchOutcome(StrEnum):
    """What one attempt to start a call did."""

    # This caller claimed the runs and Modal acknowledged the call.
    STARTED = "started"
    # Another caller claimed the runs first, or they were no longer pending.
    NOT_CLAIMED = "not_claimed"
    # Modal refused the call; the runs are failed.
    DISPATCH_FAILED = "dispatch_failed"
    # Modal acknowledged the call, but the runs had stopped dispatching before
    # the call ID could be recorded.
    NOT_RECORDED = "not_recorded"


class DispatchTransitions(Protocol):
    """The ``RunTransitions`` writes that starting a call needs."""

    def claim_simulation_dispatch(self, run_ids: Collection[UUID]) -> bool:
        """Move pending runs to dispatching."""
        ...

    def record_simulation_dispatched(
        self,
        run_ids: Collection[UUID],
        *,
        modal_call_id: str,
        first_check_at: datetime,
    ) -> bool:
        """Record the call and move the runs to running."""
        ...

    def record_simulation_dispatch_failure(
        self, run_ids: Collection[UUID], error: RunError
    ) -> bool:
        """Fail dispatching runs."""
        ...

    def claim_comparison_dispatch(self, report_run_id: UUID) -> bool:
        """Move a pending comparison step to dispatching."""
        ...

    def record_comparison_dispatched(
        self,
        report_run_id: UUID,
        *,
        modal_call_id: str,
        first_check_at: datetime,
    ) -> bool:
        """Record the comparison's call and move it to running."""
        ...

    def record_comparison_dispatch_failure(
        self, report_run_id: UUID, error: RunError
    ) -> bool:
        """Fail a dispatching comparison step."""
        ...


@dataclass(frozen=True)
class _DispatchSteps:
    """The three writes around one spawn, bound to the runs they change."""

    claim: Callable[[], bool]
    record: Callable[[str, datetime], bool]
    fail: Callable[[RunError], bool]
    # "simulation_run" or "report_run_comparison": the log event names' start.
    event_prefix: str


@dataclass
class RunDispatchService:
    """Start Modal calls for simulation runs and comparison steps."""

    transitions: DispatchTransitions
    dispatcher: CallDispatcher
    clock: Callable[[], datetime] = utc_now

    async def start_simulation_runs(
        self, run_ids: Collection[UUID], request: ModalRequest
    ) -> DispatchOutcome:
        """Start the one Modal call that computes ``run_ids``.

        Pass a household report's baseline and reform runs together: one call
        computes both, and they change status together.
        """
        runs = frozenset(run_ids)
        return await self._start(
            _DispatchSteps(
                claim=lambda: self.transitions.claim_simulation_dispatch(runs),
                record=lambda call_id, first_check_at: (
                    self.transitions.record_simulation_dispatched(
                        runs, modal_call_id=call_id, first_check_at=first_check_at
                    )
                ),
                fail=lambda error: self.transitions.record_simulation_dispatch_failure(
                    runs, error
                ),
                event_prefix="simulation_run",
            ),
            request,
            subject={"simulation_run_ids": sorted(str(run_id) for run_id in runs)},
        )

    async def start_comparison(
        self, report_run_id: UUID, request: ModalRequest
    ) -> DispatchOutcome:
        """Start an economy report run's comparison step.

        The claim succeeds only once both of its simulation runs have
        succeeded.
        """
        return await self._start(
            _DispatchSteps(
                claim=lambda: self.transitions.claim_comparison_dispatch(report_run_id),
                record=lambda call_id, first_check_at: (
                    self.transitions.record_comparison_dispatched(
                        report_run_id,
                        modal_call_id=call_id,
                        first_check_at=first_check_at,
                    )
                ),
                fail=lambda error: self.transitions.record_comparison_dispatch_failure(
                    report_run_id, error
                ),
                event_prefix="report_run_comparison",
            ),
            request,
            subject={"report_run_id": str(report_run_id)},
        )

    async def _start(
        self,
        steps: _DispatchSteps,
        request: ModalRequest,
        *,
        subject: dict[str, object],
    ) -> DispatchOutcome:
        if not await asyncio.to_thread(steps.claim):
            return DispatchOutcome.NOT_CLAIMED
        try:
            modal_call_id = await self.dispatcher.spawn(request)
        # Any failure to start the call fails the runs; they can be run again.
        except Exception as error:  # noqa: BLE001  # recorded on the runs
            logger.warning(
                _event_name(steps.event_prefix, "dispatch_failed"),
                extra={**subject, "error_type": type(error).__name__},
            )
            await asyncio.to_thread(
                steps.fail,
                RunError(
                    RunErrorCode.DISPATCH_FAILED,
                    f"Modal did not accept the call ({type(error).__name__}).",
                ),
            )
            return DispatchOutcome.DISPATCH_FAILED
        return await self._record(steps, modal_call_id, subject)

    async def _record(
        self, steps: _DispatchSteps, modal_call_id: str, subject: dict[str, object]
    ) -> DispatchOutcome:
        """Record the acknowledged call, or cancel it if it cannot be recorded.

        A call whose runs never record it would run to its end with nobody
        collecting its output, and its runs would later be failed as
        abandoned; a resubmission would then start a second call. So such a
        call is cancelled, and when the record failed, its runs are failed
        at once with dispatch_failed if the database allows.
        """
        context = {**subject, "modal_call_id": modal_call_id}
        try:
            recorded = await asyncio.to_thread(
                steps.record, modal_call_id, self.clock() + FIRST_CHECK_DELAY
            )
        except Exception as error:  # the call is cancelled instead
            logger.exception(
                _event_name(steps.event_prefix, "call_not_recorded"),
                extra={**context, "error_type": type(error).__name__},
            )
            await self._cancel(modal_call_id, context, steps.event_prefix)
            await self._fail_quietly(steps, context)
            return DispatchOutcome.NOT_RECORDED
        if not recorded:
            # The runs stopped dispatching while Modal acknowledged the call,
            # for example because the recording loop failed them as abandoned.
            logger.warning(
                _event_name(steps.event_prefix, "call_not_recorded"), extra=context
            )
            await self._cancel(modal_call_id, context, steps.event_prefix)
            return DispatchOutcome.NOT_RECORDED
        logger.info(_event_name(steps.event_prefix, "dispatched"), extra=context)
        return DispatchOutcome.STARTED

    async def _cancel(
        self, modal_call_id: str, context: dict[str, object], event_prefix: str
    ) -> None:
        try:
            await self.dispatcher.cancel(modal_call_id)
        except Exception as error:  # logged; nothing else can be done
            logger.exception(
                _event_name(event_prefix, "call_not_cancelled"),
                extra={**context, "error_type": type(error).__name__},
            )

    async def _fail_quietly(
        self, steps: _DispatchSteps, context: dict[str, object]
    ) -> None:
        try:
            await asyncio.to_thread(
                steps.fail,
                RunError(
                    RunErrorCode.DISPATCH_FAILED,
                    "The Modal call could not be recorded, so it was cancelled.",
                ),
            )
        except Exception as error:  # the abandoned-dispatch check fails the runs later
            logger.exception(
                _event_name(steps.event_prefix, "dispatch_failure_not_recorded"),
                extra={**context, "error_type": type(error).__name__},
            )
