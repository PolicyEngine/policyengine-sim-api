"""Tests for the Stage 12 runtime store's retry of refused database connections."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from policyengine_simulation_contract.stage12_execution import SimulationRole
from policyengine_simulation_executor.stage12_runtime import database_retry
from policyengine_simulation_executor.stage12_runtime.dependencies import runtime_store
from policyengine_stage12_persistence import store as persistence_store_module
from sqlalchemy.exc import OperationalError
from stage12_runtime_support import (
    TelemetryRecordingRuntime,
    child_record,
    connection_limit_error,
    parent_record,
    planned_simulation,
)


class ScriptedResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row

    def one(self):
        return self.row


class ScriptedConnection:
    """Answer each statement with the next scripted row."""

    def __init__(self, rows, *, commit_error=None):
        self.rows = list(rows)
        self.statement_count = 0
        self.commit_error = commit_error

    def __enter__(self):
        return self

    def __exit__(self, *_exception_details):
        return None

    @contextmanager
    def begin(self):
        yield
        if self.commit_error is not None:
            raise self.commit_error

    def execute(self, _statement):
        self.statement_count += 1
        return ScriptedResult(self.rows.pop(0))


class BusyPoolerEngine:
    """Refuse the first connections, as a session pooler at its limit does."""

    def __init__(self, rows, *, refused_connections=0, commit_error=None):
        self.connection = ScriptedConnection(rows, commit_error=commit_error)
        self.refused_connections = refused_connections
        self.connection_attempts = 0

    def connect(self):
        self.connection_attempts += 1
        if self.connection_attempts <= self.refused_connections:
            raise connection_limit_error()
        return self.connection


def _runtime_store_over(engine, monkeypatch, runtime):
    monkeypatch.setattr(database_retry, "INITIAL_RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(
        persistence_store_module,
        "create_stage12_engine",
        lambda _database_url: engine,
    )
    return runtime_store(runtime)


def test_runtime_store_retries_a_refused_connection_for_child_state_creation(
    monkeypatch,
) -> None:
    ordered_events: list[tuple[str, object]] = []
    child = child_record(planned_simulation(SimulationRole.BASELINE))
    engine = BusyPoolerEngine(
        [parent_record().model_dump(mode="python"), child.model_dump(mode="python")],
        refused_connections=1,
    )
    store = _runtime_store_over(
        engine, monkeypatch, TelemetryRecordingRuntime(ordered_events)
    )

    result = store.create_or_resolve_simulation(child)

    assert result.created is True
    assert result.record == child
    assert engine.connection_attempts == 2
    assert engine.connection.statement_count == 2
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
    engine = BusyPoolerEngine(
        [report.model_dump(mode="python")],
        commit_error=lost_commit_reply,
    )
    store = _runtime_store_over(
        engine, monkeypatch, TelemetryRecordingRuntime(ordered_events)
    )

    with pytest.raises(OperationalError) as failure:
        store.create_or_resolve_report(report)

    assert failure.value is lost_commit_reply
    assert engine.connection_attempts == 1
    assert engine.connection.statement_count == 1
    assert ordered_events == []
