"""Tests for retrying Stage 12 database connections that could not be opened."""

from __future__ import annotations

import logging

import pytest
from policyengine_simulation_executor.stage12_runtime import database_retry
from policyengine_simulation_executor.stage12_runtime.database_retry import (
    open_connection_with_retry,
)
from sqlalchemy.exc import OperationalError


def _connection_limit_error() -> OperationalError:
    # SQLAlchemy wraps a failure to open a connection without a statement.
    return OperationalError(
        None,
        None,
        Exception("(EMAXCONNSESSION) max clients reached in session mode"),
    )


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class RetryLogRuntime:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def log(self, message, *, severity="INFO", attributes=None, error=None):
        self.records.append(
            {
                "message": message,
                "severity": severity,
                "attributes": dict(attributes or {}),
                "error": error,
            }
        )


class OpenedConnection:
    """Stand in for the connection a successful connect step returns."""


def _upper_bound_delay(_lower_bound: float, upper_bound: float) -> float:
    return upper_bound


def test_refused_connections_are_retried_with_growing_bounded_delays() -> None:
    clock = ManualClock()
    runtime = RetryLogRuntime()
    opened_connection = OpenedConnection()
    attempts: list[int] = []

    def connect_after_two_refusals() -> OpenedConnection:
        attempts.append(len(attempts) + 1)
        if len(attempts) <= 2:
            raise _connection_limit_error()
        return opened_connection

    result = open_connection_with_retry(
        connect_after_two_refusals,
        operation_name="create_or_resolve_simulation",
        runtime=runtime,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        choose_delay=_upper_bound_delay,
    )

    assert result is opened_connection
    assert attempts == [1, 2, 3]
    assert clock.sleeps == [1.0, 2.0]
    assert [record["attributes"] for record in runtime.records] == [
        {
            "stage12_database_operation": "create_or_resolve_simulation",
            "stage12_database_failed_attempt": 1,
            "stage12_database_retry_delay_seconds": 1.0,
        },
        {
            "stage12_database_operation": "create_or_resolve_simulation",
            "stage12_database_failed_attempt": 2,
            "stage12_database_retry_delay_seconds": 2.0,
        },
    ]
    assert all(record["severity"] == "WARNING" for record in runtime.records)
    assert all(
        isinstance(record["error"], OperationalError) for record in runtime.records
    )


def test_jittered_delay_stays_between_half_and_all_of_the_ceiling() -> None:
    clock = ManualClock()
    requested_ranges: list[tuple[float, float]] = []

    def lowest_delay(lower_bound: float, upper_bound: float) -> float:
        requested_ranges.append((lower_bound, upper_bound))
        return lower_bound

    attempts: list[int] = []

    def connect_on_third_attempt() -> OpenedConnection:
        attempts.append(1)
        if len(attempts) < 3:
            raise _connection_limit_error()
        return OpenedConnection()

    open_connection_with_retry(
        connect_on_third_attempt,
        operation_name="get_simulation",
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        choose_delay=lowest_delay,
    )

    assert requested_ranges == [(0.5, 1.0), (1.0, 2.0)]
    assert clock.sleeps == [0.5, 1.0]


def test_errors_other_than_a_refused_connection_are_not_retried() -> None:
    clock = ManualClock()
    attempts: list[int] = []

    def fail_with_invalid_connection_options() -> None:
        attempts.append(1)
        msg = "invalid connection option"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="invalid connection option"):
        open_connection_with_retry(
            fail_with_invalid_connection_options,
            operation_name="get_report",
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert attempts == [1]
    assert clock.sleeps == []


def test_retries_stop_when_the_budget_is_spent() -> None:
    clock = ManualClock()
    attempts: list[int] = []

    def never_connect() -> None:
        attempts.append(1)
        raise _connection_limit_error()

    with pytest.raises(OperationalError, match="EMAXCONNSESSION"):
        open_connection_with_retry(
            never_connect,
            operation_name="create_or_resolve_report",
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            choose_delay=_upper_bound_delay,
        )

    budget = database_retry.CONNECTION_RETRY_BUDGET_SECONDS
    assert sum(clock.sleeps) == pytest.approx(budget)
    assert max(clock.sleeps) <= database_retry.MAXIMUM_RETRY_DELAY_SECONDS
    assert len(attempts) == len(clock.sleeps) + 1


def test_retry_telemetry_failure_does_not_stop_the_retry() -> None:
    class FailingRuntime:
        def log(self, *_args, **_kwargs):
            msg = "telemetry exporter unavailable"
            raise RuntimeError(msg)

    clock = ManualClock()
    opened_connection = OpenedConnection()
    attempts: list[int] = []

    def connect_on_second_attempt() -> OpenedConnection:
        attempts.append(1)
        if len(attempts) == 1:
            raise _connection_limit_error()
        return opened_connection

    assert (
        open_connection_with_retry(
            connect_on_second_attempt,
            operation_name="create_or_resolve_report",
            runtime=FailingRuntime(),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        is opened_connection
    )


def test_retry_is_logged_when_no_observability_runtime_is_available(
    caplog,
) -> None:
    clock = ManualClock()
    attempts: list[int] = []

    def connect_on_second_attempt() -> OpenedConnection:
        attempts.append(1)
        if len(attempts) == 1:
            raise _connection_limit_error()
        return OpenedConnection()

    with caplog.at_level(logging.WARNING, logger=database_retry.__name__):
        open_connection_with_retry(
            connect_on_second_attempt,
            operation_name="replace_simulation",
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            choose_delay=_upper_bound_delay,
        )

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "Stage 12 database connection unavailable for replace_simulation; "
        "retrying attempt 2 after 1.000 seconds"
    )
    assert isinstance(record.exc_info[1], OperationalError)


def test_delays_double_up_to_the_maximum_until_the_budget_is_spent() -> None:
    clock = ManualClock()

    def never_connect() -> None:
        raise _connection_limit_error()

    with pytest.raises(OperationalError):
        open_connection_with_retry(
            never_connect,
            operation_name="get_report",
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            choose_delay=_upper_bound_delay,
        )

    assert clock.sleeps == [1.0, 2.0, 4.0, 8.0, 15.0, 15.0, 15.0]


def test_the_last_delay_is_cut_to_the_remaining_budget() -> None:
    clock = ManualClock()
    seconds_per_refused_attempt = 12.0

    def slowly_refuse() -> None:
        clock.now += seconds_per_refused_attempt
        raise _connection_limit_error()

    with pytest.raises(OperationalError):
        open_connection_with_retry(
            slowly_refuse,
            operation_name="get_report",
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            choose_delay=_upper_bound_delay,
        )

    # 12 + 1 + 12 + 2 + 12 + 4 + 12 seconds leave 5 of the 60-second budget.
    assert clock.sleeps == [1.0, 2.0, 4.0, 5.0]
