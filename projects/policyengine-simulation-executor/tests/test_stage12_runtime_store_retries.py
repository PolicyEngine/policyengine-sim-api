"""Tests for the Stage 12 runtime store's retry of refused database connections."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from policyengine_simulation_contract.stage12_execution import SimulationRole
from policyengine_simulation_executor.stage12_runtime import database_retry
from policyengine_simulation_executor.stage12_runtime.dependencies import runtime_store
from policyengine_simulation_executor.stage12_runtime.household_report_recording import (
    household_report_run_store,
)
from policyengine_simulation_executor.stage12_runtime.report_run_access import (
    MODAL_WORKER_DATABASE_URL_ENV,
    budget_window_store,
    report_run_store,
)
from policyengine_stage12_persistence import engine as persistence_engine_module
from sqlalchemy.exc import OperationalError
from stage12_runtime_support import (
    TelemetryRecordingRuntime,
    child_record,
    connection_limit_error,
    parent_record,
    planned_simulation,
)


REPORT_RUN_ID = UUID("00000000-0000-0000-0000-00000000000a")
CLAIMED_AT = datetime(2026, 11, 4, tzinfo=UTC)


class ScriptedResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row

    def one(self):
        return self.row

    def scalar_one(self):
        return self.row

    def scalar_one_or_none(self):
        return self.row


class ScriptedSession:
    """Answer each statement after the first with the next scripted row.

    As on a real session, the first statement checks out the connection, which
    the pooler may refuse.
    """

    def __init__(self, pooler):
        self.pooler = pooler
        self.connected = False

    def __enter__(self):
        return self

    def __exit__(self, *_exception_details):
        return None

    def close(self):
        return None

    def commit(self):
        if self.pooler.commit_error is not None:
            raise self.pooler.commit_error

    def execute(self, _statement, *_parameters, **_options):
        if not self.connected:
            self.pooler.connect()
            self.connected = True
            return ScriptedResult(True)
        self.pooler.statement_count += 1
        return ScriptedResult(self.pooler.rows.pop(0))


class BusyPooler:
    """A session factory refusing the first connections, as a full pooler does."""

    def __init__(self, rows, *, refused_connections=0, commit_error=None):
        self.rows = list(rows)
        self.statement_count = 0
        self.commit_error = commit_error
        self.refused_connections = refused_connections
        self.connection_attempts = 0

    def __call__(self):
        return ScriptedSession(self)

    def connect(self):
        self.connection_attempts += 1
        if self.connection_attempts <= self.refused_connections:
            raise connection_limit_error()


def _use_pooler(pooler, monkeypatch) -> None:
    monkeypatch.setattr(database_retry, "INITIAL_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(
        persistence_engine_module,
        "create_stage12_session_factory",
        lambda _database_url: pooler,
    )


def _runtime_store_over(pooler, monkeypatch, runtime):
    _use_pooler(pooler, monkeypatch)
    return runtime_store(runtime)


def test_runtime_store_retries_a_refused_connection_for_child_state_creation(
    monkeypatch,
) -> None:
    ordered_events: list[tuple[str, object]] = []
    child = child_record(planned_simulation(SimulationRole.BASELINE))
    pooler = BusyPooler(
        [parent_record().model_dump(mode="python"), child.model_dump(mode="python")],
        refused_connections=1,
    )
    store = _runtime_store_over(
        pooler, monkeypatch, TelemetryRecordingRuntime(ordered_events)
    )

    result = store.create_or_resolve_simulation(child)

    assert result.created is True
    assert result.record == child
    assert pooler.connection_attempts == 2
    assert pooler.statement_count == 2
    [(event_kind, (message, attributes, error))] = ordered_events
    assert event_kind == "telemetry"
    assert message == "Stage 12 database connection unavailable; retrying"
    assert attributes["stage12_database_operation"] == "create_or_resolve_simulation"
    assert attributes["stage12_database_failed_attempt"] == 1
    assert isinstance(error, OperationalError)


def test_runtime_store_does_not_retry_a_failed_commit(monkeypatch) -> None:
    ordered_events: list[tuple[str, object]] = []
    lost_commit_reply = OperationalError(
        None,
        None,
        Exception("server closed the connection unexpectedly"),
    )
    report = parent_record()
    pooler = BusyPooler(
        [report.model_dump(mode="python")],
        commit_error=lost_commit_reply,
    )
    store = _runtime_store_over(
        pooler, monkeypatch, TelemetryRecordingRuntime(ordered_events)
    )

    with pytest.raises(OperationalError) as failure:
        store.create_or_resolve_report(report)

    assert failure.value is lost_commit_reply
    assert pooler.connection_attempts == 1
    assert pooler.statement_count == 1
    assert ordered_events == []


def _claim_a_report_run(store) -> Any:
    return store.claim_report_run(
        REPORT_RUN_ID, modal_call_id="fc-call", claimed_at=CLAIMED_AT
    )


@pytest.mark.parametrize(
    ("build_store", "write"),
    [
        pytest.param(report_run_store, _claim_a_report_run, id="report-runs"),
        pytest.param(
            household_report_run_store, _claim_a_report_run, id="household-reports"
        ),
        pytest.param(
            budget_window_store,
            lambda store: store.window_of_year(REPORT_RUN_ID),
            id="budget-windows",
        ),
    ],
)
def test_the_modal_worker_stores_retry_a_refused_connection(
    build_store, write, monkeypatch
) -> None:
    ordered_events: list[tuple[str, object]] = []
    monkeypatch.setenv(MODAL_WORKER_DATABASE_URL_ENV, "postgresql://user@host/database")
    pooler = BusyPooler(["running"], refused_connections=1)
    _use_pooler(pooler, monkeypatch)

    write(build_store(TelemetryRecordingRuntime(ordered_events)))

    assert pooler.connection_attempts == 2
    assert pooler.statement_count == 1
    [(event_kind, _event)] = ordered_events
    assert event_kind == "telemetry"
