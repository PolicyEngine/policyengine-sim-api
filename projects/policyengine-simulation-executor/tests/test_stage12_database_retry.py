"""Tests for retrying Stage 12 database work that could not connect."""

from __future__ import annotations

import pytest
from policyengine_simulation_executor.stage12_runtime import database_retry
from policyengine_simulation_executor.stage12_runtime.database_retry import (
    retry_when_connection_unavailable,
)
from sqlalchemy.exc import OperationalError


def _connection_limit_error() -> OperationalError:
    # SQLAlchemy wraps a failure to open a connection without a statement.
    return OperationalError(
        None,
        None,
        Exception("(EMAXCONNSESSION) max clients reached in session mode"),
    )


def _failed_statement_error() -> OperationalError:
    return OperationalError(
        "UPDATE comparison_reports SET status = %(status)s",
        {"status": "failed"},
        Exception("server closed the connection unexpectedly"),
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


def _upper_bound_delay(lower_bound: float, upper_bound: float) -> float:
    return upper_bound


def test_connection_failures_are_retried_with_growing_bounded_delays() -> None:
    clock = ManualClock()
    runtime = RetryLogRuntime()
    attempts: list[int] = []

    def claim_after_two_connection_failures() -> str:
        attempts.append(len(attempts) + 1)
        if len(attempts) <= 2:
            raise _connection_limit_error()
        return "claimed"

    result = retry_when_connection_unavailable(
        claim_after_two_connection_failures,
        operation_name="coordinator_claim",
        runtime=runtime,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        choose_delay=_upper_bound_delay,
    )

    assert result == "claimed"
    assert attempts == [1, 2, 3]
    assert clock.sleeps == [1.0, 2.0]
    assert [record["attributes"] for record in runtime.records] == [
        {
            "stage12_database_operation": "coordinator_claim",
            "stage12_database_failed_attempt": 1,
            "stage12_database_retry_delay_seconds": 1.0,
        },
        {
            "stage12_database_operation": "coordinator_claim",
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

    def connect_on_third_attempt() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise _connection_limit_error()
        return "connected"

    retry_when_connection_unavailable(
        connect_on_third_attempt,
        operation_name="simulation_claim",
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        choose_delay=lowest_delay,
    )

    assert requested_ranges == [(0.5, 1.0), (1.0, 2.0)]
    assert clock.sleeps == [0.5, 1.0]


@pytest.mark.parametrize(
    "error_factory",
    [_failed_statement_error, lambda: ValueError("invalid comparison record")],
)
def test_statement_and_application_errors_are_not_retried(error_factory) -> None:
    clock = ManualClock()
    attempts: list[int] = []

    def fail_without_connection_problem() -> None:
        attempts.append(1)
        raise error_factory()

    with pytest.raises(type(error_factory())):
        retry_when_connection_unavailable(
            fail_without_connection_problem,
            operation_name="coordinator_claim",
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
        retry_when_connection_unavailable(
            never_connect,
            operation_name="coordinator_claim",
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
            raise RuntimeError("telemetry exporter unavailable")

    clock = ManualClock()
    attempts: list[int] = []

    def connect_on_second_attempt() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise _connection_limit_error()
        return "connected"

    assert (
        retry_when_connection_unavailable(
            connect_on_second_attempt,
            operation_name="coordinator_claim",
            runtime=FailingRuntime(),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        == "connected"
    )
