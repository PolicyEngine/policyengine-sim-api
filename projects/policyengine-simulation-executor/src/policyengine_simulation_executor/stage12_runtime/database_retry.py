"""Bounded retry for Stage 12 database work that could not open a connection."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from contextlib import suppress
from typing import TypeVar

from policyengine_observability import ObservabilityRuntime
from policyengine_stage12_persistence import is_connection_unavailable_error

logger = logging.getLogger(__name__)

OperationResult = TypeVar("OperationResult")

# Coordinators and workers start in bursts against a session pooler with a
# fixed client limit. A short, jittered wait lets concurrent starts spread out
# instead of failing a report before any simulation work has begun.
CONNECTION_RETRY_BUDGET_SECONDS = 60.0
INITIAL_RETRY_DELAY_SECONDS = 1.0
MAXIMUM_RETRY_DELAY_SECONDS = 15.0
RETRY_DELAY_GROWTH_FACTOR = 2.0


def retry_when_connection_unavailable(
    operation: Callable[[], OperationResult],
    *,
    operation_name: str,
    runtime: ObservabilityRuntime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    choose_delay: Callable[[float, float], float] = random.uniform,
) -> OperationResult:
    """Run ``operation``, retrying only failures to open a database connection.

    The database received no statement when a connection could not be opened,
    so repeating the operation cannot duplicate a write. Every other error,
    including a failed statement, propagates immediately. Retries stop once
    the retry budget is spent and the final connection error propagates.
    """

    started = monotonic()
    attempt = 1
    delay_ceiling = INITIAL_RETRY_DELAY_SECONDS
    while True:
        try:
            return operation()
        except Exception as error:
            if not is_connection_unavailable_error(error):
                raise
            remaining_budget = CONNECTION_RETRY_BUDGET_SECONDS - (monotonic() - started)
            if remaining_budget <= 0:
                raise
            delay = min(
                choose_delay(delay_ceiling / 2, delay_ceiling), remaining_budget
            )
            _report_connection_retry(
                runtime,
                operation_name=operation_name,
                failed_attempt=attempt,
                delay_seconds=delay,
                error=error,
            )
            sleep(delay)
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
