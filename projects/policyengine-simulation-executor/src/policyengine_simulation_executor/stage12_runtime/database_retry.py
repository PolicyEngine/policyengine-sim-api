"""Bounded retry for Stage 12 database connections that could not be opened."""

from __future__ import annotations

import logging
import random
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Callable

    from policyengine_observability import ObservabilityRuntime
    from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

# Coordinators and workers start in bursts against a session pooler with a
# fixed client limit. A short, jittered wait lets concurrent connections
# spread out instead of failing a report on one refused connection.
CONNECTION_RETRY_BUDGET_SECONDS = 60.0
INITIAL_RETRY_DELAY_SECONDS = 1.0
MAXIMUM_RETRY_DELAY_SECONDS = 15.0
RETRY_DELAY_GROWTH_FACTOR = 2.0


@dataclass(frozen=True)
class RetryTiming:
    """The clock, sleep and jitter a connection retry uses; tests replace them.

    ``choose_delay`` picks a delay between its lower and upper bound.
    """

    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    choose_delay: Callable[[float, float], float] = random.uniform


def open_connection_with_retry(
    connect: Callable[[], Connection],
    *,
    operation_name: str,
    runtime: ObservabilityRuntime | None = None,
    timing: RetryTiming | None = None,
) -> Connection:
    """Open a Stage 12 database connection, retrying while it is refused.

    The persistence store passes only its connect step here. Failing that step
    means no connection opened and no statement reached the database, so
    opening it again cannot duplicate a write. The store runs the operation's
    statements and its COMMIT or ROLLBACK after this returns; their failures,
    including a COMMIT whose reply was lost, propagate without a retry.
    Errors other than ``OperationalError`` propagate immediately, and retries
    stop once the retry budget is spent.
    """
    timing = timing or RetryTiming()
    started = timing.monotonic()
    attempt = 1
    delay_ceiling = INITIAL_RETRY_DELAY_SECONDS
    while True:
        try:
            return connect()
        except OperationalError as error:
            remaining_budget = CONNECTION_RETRY_BUDGET_SECONDS - (
                timing.monotonic() - started
            )
            if remaining_budget <= 0:
                raise
            delay = min(
                timing.choose_delay(delay_ceiling / 2, delay_ceiling), remaining_budget
            )
            _report_connection_retry(
                runtime,
                operation_name=operation_name,
                failed_attempt=attempt,
                delay_seconds=delay,
                error=error,
            )
            timing.sleep(delay)
            attempt += 1
            delay_ceiling = min(
                delay_ceiling * RETRY_DELAY_GROWTH_FACTOR,
                MAXIMUM_RETRY_DELAY_SECONDS,
            )


def _report_connection_retry(
    runtime: ObservabilityRuntime | None,
    *,
    operation_name: str,
    failed_attempt: int,
    delay_seconds: float,
    error: Exception,
) -> None:
    if runtime is None:
        logger.warning(
            "Stage 12 database connection unavailable for %s; retrying "
            "attempt %d after %.3f seconds",
            operation_name,
            failed_attempt + 1,
            delay_seconds,
            exc_info=error,
        )
        return
    # Telemetry must not change whether the operation is retried.
    with suppress(Exception):
        runtime.log(
            "Stage 12 database connection unavailable; retrying",
            severity="WARNING",
            attributes={
                "stage12_database_operation": operation_name,
                "stage12_database_failed_attempt": failed_attempt,
                "stage12_database_retry_delay_seconds": round(delay_seconds, 3),
            },
            error=error,
        )
