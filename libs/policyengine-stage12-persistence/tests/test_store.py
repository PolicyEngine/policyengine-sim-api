from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    SimulationRole,
)
from policyengine_stage12_persistence import Stage12PersistenceStore
from policyengine_stage12_persistence import store as store_module
from policyengine_stage12_persistence.store import normalized_database_url
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import QueuePool
from sqlalchemy.sql import ClauseElement

NOW = datetime(2026, 9, 22, tzinfo=UTC)
REPORT_ID = UUID("00000000-0000-0000-0000-000000000001")
SIMULATION_ID = UUID("00000000-0000-0000-0000-000000000002")
OBSERVABILITY_ID = "00000000-0000-4000-8000-000000000012"


@pytest.mark.parametrize("fail_transaction", [False, True])
def test_runtime_engine_closes_connections_between_transactions(
    monkeypatch, fail_transaction
) -> None:
    def local_engine(_url, **options):
        # Exercise the runtime pooling configuration with a real local DBAPI.
        # PostgreSQL's default is QueuePool; SQLite's memory default differs.
        options.pop("connect_args")
        options.setdefault("poolclass", QueuePool)
        return create_engine("sqlite://", **options)

    monkeypatch.setattr(store_module, "create_engine", local_engine)
    engine = store_module.create_stage12_engine(
        "postgresql://runtime:secret@db.example/postgres"
    )
    try:
        try:
            with engine.begin() as connection:
                physical_connection = connection.connection.driver_connection
                assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
                if fail_transaction:
                    raise RuntimeError("calculation failed")
        except RuntimeError:
            assert fail_transaction

        # Long calculations must not hold an idle session after DB work ends.
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            physical_connection.execute("SELECT 1")

        with engine.begin() as connection:
            assert connection.connection.driver_connection is not physical_connection
            assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
    finally:
        engine.dispose()


def _report() -> ComparisonReportRecord:
    return ComparisonReportRecord(
        evaluation_id=REPORT_ID,
        status=ComparisonRunLifecycleStatus.PENDING,
        aggregation_status=ComparisonRunAggregationStatus.NOT_STARTED,
        environment="staging",
        calculation_flow="economy",
        originating_request_id="request-1",
        observability_id=OBSERVABILITY_ID,
        production_identity="job-1",
        incumbent_execution_id="job-1",
        worker_version="5.2.0",
        modal_application="policyengine-simulation-v2-py5-2-0",
        report_coordinator_callable="coordinate_report",
        version_manifest_sha256="a" * 64,
        policyengine_version="5.2.0",
        country_package_name="policyengine-us",
        country_package_version="1.764.6",
        country="us",
        dataset_identity="populace_us_2024",
        dataset_uri="hf://policyengine/data/populace_us_2024.h5@revision",
        data_package_name="populace-data",
        data_package_version="0.1.0",
        data_artifact_revision="revision",
        created_at=NOW,
        updated_at=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )


def _simulation() -> ComparisonSimulationRecord:
    return ComparisonSimulationRecord(
        simulation_execution_id=SIMULATION_ID,
        evaluation_id=REPORT_ID,
        role=SimulationRole.BASELINE,
        input_sha256="b" * 64,
        worker_version="5.2.0",
        modal_application="policyengine-simulation-v2-py5-2-0",
        simulation_callable="run_single_simulation_us",
        version_manifest_sha256="a" * 64,
        modal_invocation_id="dispatch-pending-1",
        status=ComparisonRunLifecycleStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )


def _row(record) -> dict:
    return record.model_dump(mode="python")


class FakeResult:
    def __init__(self, *, row=None, rows=None):
        self.row = row
        self.rows = rows

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row

    def one(self):
        assert self.row is not None
        return self.row

    def all(self):
        return self.rows or []


def _refused_connection_error() -> OperationalError:
    # SQLAlchemy wraps a DBAPI failure raised while connecting without a
    # statement, exactly as it wraps a failed COMMIT.
    return OperationalError(
        None,
        None,
        Exception("(EMAXCONNSESSION) max clients reached in session mode"),
    )


class FakeConnection:
    def __init__(self, results, *, commit_error=None):
        self.results = list(results)
        self.statements = []
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

    def execute(self, statement):
        assert isinstance(statement, ClauseElement)
        self.statements.append(statement)
        return self.results.pop(0)


class FakeEngine:
    def __init__(self, results, *, refused_connections=0, commit_error=None):
        self.connection = FakeConnection(results, commit_error=commit_error)
        self.refused_connections = refused_connections
        self.connection_attempts = 0

    def connect(self):
        self.connection_attempts += 1
        if self.connection_attempts <= self.refused_connections:
            raise _refused_connection_error()
        return self.connection


class RetryingConnectionOpener:
    """Record each operation's connection and retry one refused connection."""

    def __init__(self):
        self.operation_names = []
        self.refusals = []

    def __call__(self, connect, *, operation_name):
        self.operation_names.append(operation_name)
        try:
            return connect()
        except OperationalError as refusal:
            self.refusals.append(refusal)
            return connect()


def _sqlalchemy_commit_failure(monkeypatch) -> OperationalError:
    """Return the error SQLAlchemy raises when a COMMIT's reply is lost."""

    engine = create_engine("sqlite://")

    def commit_then_lose_the_reply(dbapi_connection):
        dbapi_connection.commit()
        raise sqlite3.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(engine.dialect, "do_commit", commit_then_lose_the_reply)
    try:
        with (
            pytest.raises(OperationalError) as commit_failure,
            engine.begin() as connection,
        ):
            connection.exec_driver_sql("SELECT 1")
    finally:
        engine.dispose()
    return commit_failure.value


def test_operations_open_connections_through_the_opener_before_any_statement() -> None:
    report = _report()
    simulation = _simulation()
    engine = FakeEngine(
        [
            FakeResult(row=_row(report)),
            FakeResult(row=_row(simulation)),
            FakeResult(row=_row(simulation)),
        ],
        refused_connections=1,
    )
    opener = RetryingConnectionOpener()
    store = Stage12PersistenceStore(
        engine=engine,  # type: ignore[arg-type]
        open_connection=opener,
    )

    created = store.create_or_resolve_simulation(simulation)
    read = store.get_simulation(SIMULATION_ID)

    assert created.record == simulation
    assert read == simulation
    assert opener.operation_names == ["create_or_resolve_simulation", "get_simulation"]
    # The refusal reached the opener before the store sent any statement.
    assert len(opener.refusals) == 1
    assert engine.connection_attempts == 3
    assert len(engine.connection.statements) == 3


def test_commit_failure_is_raised_after_the_opener_returns(monkeypatch) -> None:
    # A failed COMMIT carries no statement, like a refused connection, but the
    # write may already be durable. Only where it occurs tells them apart.
    lost_commit_reply = _sqlalchemy_commit_failure(monkeypatch)
    assert lost_commit_reply.statement is None
    report = _report()
    engine = FakeEngine(
        [FakeResult(row=_row(report))],
        commit_error=lost_commit_reply,
    )
    opener = RetryingConnectionOpener()
    store = Stage12PersistenceStore(
        engine=engine,  # type: ignore[arg-type]
        open_connection=opener,
    )

    with pytest.raises(OperationalError) as failure:
        store.create_or_resolve_report(report)

    assert failure.value is lost_commit_reply
    assert opener.operation_names == ["create_or_resolve_report"]
    assert opener.refusals == []
    assert engine.connection_attempts == 1
    assert len(engine.connection.statements) == 1


def test_default_store_does_not_retry_a_refused_connection() -> None:
    engine = FakeEngine([], refused_connections=1)
    store = Stage12PersistenceStore(engine=engine)  # type: ignore[arg-type]

    with pytest.raises(OperationalError, match="EMAXCONNSESSION"):
        store.get_report(REPORT_ID)

    assert engine.connection_attempts == 1


def test_create_and_read_report_use_sqlalchemy_statements_only() -> None:
    report = _report()
    engine = FakeEngine([FakeResult(row=_row(report))])
    store = Stage12PersistenceStore(engine=engine)  # type: ignore[arg-type]

    result = store.create_or_resolve_report(report)

    assert result.created is True
    assert result.record == report
    assert result.record.observability_id == OBSERVABILITY_ID
    assert len(engine.connection.statements) == 1
    assert "observability_id" in str(engine.connection.statements[0])


def test_report_observability_identifier_is_limited_to_36_characters() -> None:
    values = _report().model_dump(mode="python")
    values["observability_id"] = "x" * 37

    with pytest.raises(ValueError, match="at most 36 characters"):
        ComparisonReportRecord.model_validate(values)


def test_report_identity_conflict_resolves_existing_row() -> None:
    report = _report()
    engine = FakeEngine(
        [
            FakeResult(row=None),
            FakeResult(row=_row(report)),
        ]
    )
    store = Stage12PersistenceStore(engine=engine)  # type: ignore[arg-type]

    result = store.create_or_resolve_report(report)

    assert result.created is False
    assert result.record == report


def test_simulation_creation_checks_the_parent_in_the_same_transaction() -> None:
    report = _report()
    simulation = _simulation()
    engine = FakeEngine(
        [
            FakeResult(row=_row(report)),
            FakeResult(row=_row(simulation)),
        ]
    )
    store = Stage12PersistenceStore(engine=engine)  # type: ignore[arg-type]

    result = store.create_or_resolve_simulation(simulation)

    assert result.created is True
    assert result.record == simulation
    assert len(engine.connection.statements) == 2


def test_invalid_lifecycle_transition_is_rejected_before_update() -> None:
    existing = _report()
    candidate = existing.model_copy(
        update={"status": ComparisonRunLifecycleStatus.SUCCEEDED}
    )
    engine = FakeEngine([FakeResult(row=_row(existing))])
    store = Stage12PersistenceStore(engine=engine)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="lifecycle cannot move"):
        store.replace_report(candidate)

    assert len(engine.connection.statements) == 1


def test_invocation_attachment_is_compare_and_set() -> None:
    attached = _simulation().model_copy(
        update={"modal_invocation_id": "fc-123", "updated_at": NOW}
    )
    engine = FakeEngine([FakeResult(row=_row(attached))])
    store = Stage12PersistenceStore(engine=engine)  # type: ignore[arg-type]

    result = store.attach_simulation_invocation(
        SIMULATION_ID,
        expected_placeholder="dispatch-pending-1",
        modal_invocation_id="fc-123",
        updated_at=NOW,
    )

    assert result == attached


def test_simulation_status_replacement_is_compare_and_set() -> None:
    running = _simulation().model_copy(
        update={"status": ComparisonRunLifecycleStatus.RUNNING}
    )
    failed = running.model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.FAILED,
            "error_code": "simulation_execution_failed",
            "completed_at": NOW,
        }
    )
    engine = FakeEngine(
        [
            FakeResult(row=_row(running)),
            FakeResult(row=_row(failed)),
        ]
    )
    opener = RetryingConnectionOpener()
    store = Stage12PersistenceStore(
        engine=engine,  # type: ignore[arg-type]
        open_connection=opener,
    )

    result, replaced = store.replace_simulation_if_status(
        failed,
        expected_status=ComparisonRunLifecycleStatus.RUNNING,
    )

    assert replaced is True
    assert result == failed
    assert opener.operation_names == ["replace_simulation_if_status"]
    assert len(engine.connection.statements) == 2


def test_simulation_status_replacement_preserves_a_concurrent_cancellation() -> None:
    incomplete = _simulation().model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.INCOMPLETE,
            "error_code": "cancelled_after_peer_failure",
            "completed_at": NOW,
        }
    )
    succeeded = _simulation().model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.SUCCEEDED,
            "output_uri": "gs://private/baseline.parquet",
            "output_sha256": "c" * 64,
            "output_schema_version": 1,
            "row_identity_columns": ("household.household_id",),
            "row_count": 1,
            "row_identity_sha256": "d" * 64,
            "completed_at": NOW,
        }
    )
    engine = FakeEngine([FakeResult(row=_row(incomplete))])
    store = Stage12PersistenceStore(engine=engine)  # type: ignore[arg-type]

    result, replaced = store.replace_simulation_if_status(
        succeeded,
        expected_status=ComparisonRunLifecycleStatus.RUNNING,
    )

    assert replaced is False
    assert result == incomplete
    assert len(engine.connection.statements) == 1


def test_database_url_uses_one_canonical_secret_format() -> None:
    parsed = normalized_database_url(
        "postgresql://policyengine_v2_runtime:secret@db.example/postgres"
    )
    assert parsed.drivername == "postgresql+psycopg"
    assert parsed.username == "policyengine_v2_runtime"

    with pytest.raises(ValueError, match="postgresql://"):
        normalized_database_url(
            "postgresql+psycopg://policyengine_v2_runtime:secret@db.example/postgres"
        )
