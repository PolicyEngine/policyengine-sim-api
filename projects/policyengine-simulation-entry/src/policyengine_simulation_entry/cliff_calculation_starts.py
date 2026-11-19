"""Start the cliff calculations a report's request created, one per simulation run.

A simulation run has one cliff result, its cliff gap and cliff share, which
every report naming the run reads: cliffs are a calculation of the run, not
of any report (C1 · 24's answer 4b, API v1's ``cliff_impacts``).
``include_cliffs`` is a request option, never stored or keyed. A create or
re-run asking for cliffs ends on a report run as any other does, and in the
same transaction finds or creates the cliff calculation of each simulation
run that report run names (``find_or_insert_report_run_cliffs``): a run's
calculation that hasn't failed, pending, running or succeeded, is joined,
and a failed one never counts. So two identical requests end on one
calculation per run, and a request the database refuses wrote neither the
report run nor its calculations.

This module starts the calculations the request created, and only those,
before the report run's coordinator: they were written with the report
run, so they are started even when Modal then refuses the coordinator. It
starts the simulation run's simulation function, given the run's own input
and a context naming the calculation (C1 · 38's cliff mode), called with
``spawn``. It records the call's ID while the calculation is pending with
none. When Modal refuses the call, or doesn't confirm it in time, it fails
the calculation while it is still pending with no call, so the next request
asking for cliffs starts another; one the function claimed meanwhile is
left as it is and answered ``pending``. The report run is not affected
either way. A calculation runs beside its simulation run, whatever that
run's state: it recalculates the simulation from the same dataset and
policy, and doesn't read the run's output.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

from policyengine_simulation_observability.log_records import (
    database_row_attributes,
    log_extra,
)
from policyengine_simulation_persistence import SimulationStatus
from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_entry.economy_api_models import (
    CliffCalculationState,
    ReportCliffs,
)
from policyengine_simulation_entry.economy_calculation_inputs import (
    cliff_calculation,
)

if TYPE_CHECKING:
    from uuid import UUID

    from policyengine_simulation_persistence import (
        FoundCliffCalculation,
        FoundCliffs,
    )

    from policyengine_simulation_entry.economy_calculation_inputs import (
        CalculationScope,
        CallOrigin,
    )
    from policyengine_simulation_entry.economy_workers import (
        EconomyRunCaller,
        EconomyWorker,
    )

logger = logging.getLogger(__name__)


class CliffCalculationRuns(Protocol):
    """The store's writes of cliff calculations (``EconomyRunStore``)."""

    def record_cliff_call(
        self, cliff_result_id: UUID, modal_call_id: str
    ) -> SimulationStatus | None:
        """Record the calculation's call ID while it is pending with none."""
        ...

    def fail_unstarted_cliff_calculation(
        self, cliff_result_id: UUID, error_message: str
    ) -> bool:
        """Fail the calculation while it is pending with no call ID."""
        ...


class CliffCalculationStarts:
    """Start the cliff calculations a request created, and answer each side's."""

    def __init__(
        self,
        *,
        store: CliffCalculationRuns,
        caller: EconomyRunCaller,
        call_start_timeout_seconds: float,
    ) -> None:
        """Record calls in ``store``; wait this long for Modal to accept each."""
        self._store = store
        self._caller = caller
        self._call_start_timeout_seconds = call_start_timeout_seconds

    async def start(  # noqa: PLR0913  # what each call calculates, then each side
        self,
        worker: EconomyWorker,
        scope: CalculationScope,
        origin: CallOrigin,
        *,
        found: FoundCliffs,
        baseline_policy: dict[str, Any],
        reform_policy: dict[str, Any],
        observability_context: dict[str, Any] | None,
    ) -> ReportCliffs:
        """Start each calculation in ``found`` this request created; answer each side.

        ``found`` holds each side's run's calculation as the store found or
        created it with the report run, and each policy is the one that
        side's run calculates. One the store found is answered with its
        status as read, and gets no call; one run on both sides has one
        calculation, started once.
        """
        sides = {
            found.reform.simulation_run_id: (found.reform, reform_policy),
            # The baseline's last, so one run on both sides keeps its policy.
            found.baseline.simulation_run_id: (found.baseline, baseline_policy),
        }
        created = [
            (calculation, policy)
            for calculation, policy in sides.values()
            if calculation.inserted
        ]
        started = await asyncio.gather(
            *(
                self._start_one(
                    worker,
                    scope,
                    origin,
                    calculation=calculation,
                    policy=policy,
                    observability_context=observability_context,
                )
                for calculation, policy in created
            )
        )
        statuses = {
            calculation.simulation_run_id: status
            for (calculation, _), status in zip(created, started, strict=True)
        }

        def state(calculation: FoundCliffCalculation) -> CliffCalculationState:
            return CliffCalculationState(
                cliff_result_id=calculation.id,
                status=statuses.get(calculation.simulation_run_id, calculation.status),
            )

        return ReportCliffs(baseline=state(found.baseline), reform=state(found.reform))

    async def _start_one(  # noqa: PLR0913  # what the call calculates, then its row
        self,
        worker: EconomyWorker,
        scope: CalculationScope,
        origin: CallOrigin,
        *,
        calculation: FoundCliffCalculation,
        policy: dict[str, Any],
        observability_context: dict[str, Any] | None,
    ) -> SimulationStatus:
        """Start one calculation and record its call; or fail it, unless started.

        Return its status: as recorded, ``pending`` when the record couldn't
        be written or the function claimed it first, ``failed`` when the
        call wasn't confirmed and the calculation was failed.
        """
        cliff_result_id = calculation.id
        call = cliff_calculation(
            worker,
            scope,
            origin,
            simulation_run_id=calculation.simulation_run_id,
            cliff_result_id=cliff_result_id,
            policy=policy,
        )
        try:
            call_id = await asyncio.wait_for(
                self._caller.start_cliff_calculation(
                    worker,
                    call.payload,
                    call.context,
                    observability_context=observability_context,
                ),
                timeout=self._call_start_timeout_seconds,
            )
        # Modal raises its own errors; any of them, or the timeout, means the
        # call was not confirmed, and the calculation is failed unless started.
        except Exception as error:  # noqa: BLE001  # every unconfirmed call is answered alike
            failed = await self._fail_unconfirmed(cliff_result_id, error)
            return SimulationStatus.FAILED if failed else SimulationStatus.PENDING
        try:
            status = await asyncio.to_thread(
                self._store.record_cliff_call, cliff_result_id, call_id
            )
        except SQLAlchemyError as error:
            # The function claims a pending calculation with no call ID itself.
            logger.warning(
                "cliff_calculation_call_not_recorded",
                extra=log_extra(
                    error_type=type(error).__name__,
                    **database_row_attributes("cliff_impacts", cliff_result_id),
                ),
            )
            return SimulationStatus.PENDING
        return SimulationStatus.PENDING if status is None else status

    async def _fail_unconfirmed(
        self, cliff_result_id: UUID, error: BaseException
    ) -> bool:
        """Fail the calculation of a call Modal didn't confirm, unless it started.

        Return whether it was failed. When the write itself fails, the
        calculation stays pending, and the stuck-run sweep fails it if no
        call ever claims it.
        """
        # The log names the error type only; its text could repeat request data.
        logger.error(
            "cliff_calculation_call_unconfirmed",
            extra=log_extra(
                error_type=type(error).__name__,
                **database_row_attributes("cliff_impacts", cliff_result_id),
            ),
        )
        message = (
            "modal_call_refused: Modal did not accept the cliff calculation's "
            f"call ({type(error).__name__})."
        )
        try:
            return await asyncio.to_thread(
                self._store.fail_unstarted_cliff_calculation, cliff_result_id, message
            )
        except SQLAlchemyError as write_error:
            logger.warning(
                "cliff_calculation_not_failed",
                extra=log_extra(
                    error_type=type(write_error).__name__,
                    **database_row_attributes("cliff_impacts", cliff_result_id),
                ),
            )
            return False
