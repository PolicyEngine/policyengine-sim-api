"""Static and behavioral tests for Stage 12 DML-only persistence."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from policyengine_simulation_contract.stage12_execution import (
    EvaluationAggregationStatus,
    EvaluationLifecycleStatus,
    EvaluationReportRecord,
    EvaluationSimulationRecord,
    SimulationRole,
)
from policyengine_simulation_contract.stage12_persistence import (
    PostgresEvaluationStore,
    sql_statements,
)

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def test_persistence_contract_imports_without_postgres_extra() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.modules['psycopg'] = None; "
                "import policyengine_simulation_contract.stage12_persistence"
            ),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def _record() -> EvaluationReportRecord:
    return EvaluationReportRecord(
        evaluation_id=UUID("00000000-0000-0000-0000-000000000001"),
        status=EvaluationLifecycleStatus.PENDING,
        aggregation_status=EvaluationAggregationStatus.NOT_STARTED,
        environment="staging",
        calculation_flow="economy",
        originating_request_id="request-1",
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


def _simulation_record(role: SimulationRole) -> EvaluationSimulationRecord:
    suffix = 2 if role is SimulationRole.BASELINE else 3
    return EvaluationSimulationRecord(
        simulation_execution_id=UUID(f"00000000-0000-0000-0000-{suffix:012d}"),
        evaluation_id=_record().evaluation_id,
        role=role,
        input_sha256=("a" if role is SimulationRole.BASELINE else "b") * 64,
        worker_version="5.2.0",
        modal_application="policyengine-simulation-v2-py5-2-0",
        simulation_callable="run_single_simulation_us",
        version_manifest_sha256="a" * 64,
        status=EvaluationLifecycleStatus.RUNNING,
        created_at=NOW,
        updated_at=NOW,
        started_at=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, statement, parameters):
        self.calls.append((statement, parameters))

    def fetchone(self):
        return self.rows.pop(0)

    def fetchall(self):
        rows = self.rows
        self.rows = []
        return rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def cursor(self):
        return self._cursor


def test_adapter_contains_only_dml_statements() -> None:
    statements = sql_statements()
    assert statements
    for statement in statements.values():
        upper = statement.upper()
        assert upper.startswith(("SELECT", "INSERT", "UPDATE", "DELETE"))
        assert "CREATE " not in upper
        assert "ALTER " not in upper
        assert "DROP " not in upper
        assert "TRUNCATE " not in upper


def test_create_or_resolve_report_returns_inserted_record() -> None:
    record = _record()
    cursor = FakeCursor([record.model_dump(mode="python")])
    store = PostgresEvaluationStore(
        "postgresql://runtime",
        connect=lambda *_, **__: FakeConnection(cursor),
    )

    result = store.create_or_resolve_report(record)

    assert result.created is True
    assert result.record == record
    assert len(cursor.calls) == 1
    assert cursor.calls[0][0].startswith("INSERT INTO")


def test_create_or_resolve_report_reuses_matching_conflict() -> None:
    record = _record()
    cursor = FakeCursor([None, record.model_dump(mode="python")])
    store = PostgresEvaluationStore(
        "postgresql://runtime",
        connect=lambda *_, **__: FakeConnection(cursor),
    )

    result = store.create_or_resolve_report(record)

    assert result.created is False
    assert result.record == record
    assert [call[0].split(maxsplit=1)[0] for call in cursor.calls] == [
        "INSERT",
        "SELECT",
    ]


def test_create_simulation_validates_parent_before_insert() -> None:
    parent = _record()
    child = _simulation_record(SimulationRole.BASELINE)
    cursor = FakeCursor(
        [
            parent.model_dump(mode="python"),
            child.model_dump(mode="python"),
        ]
    )
    store = PostgresEvaluationStore(
        "postgresql://runtime",
        connect=lambda *_, **__: FakeConnection(cursor),
    )

    result = store.create_or_resolve_simulation(child)

    assert result.created is True
    assert result.record == child
    assert [call[0].split(maxsplit=1)[0] for call in cursor.calls] == [
        "SELECT",
        "INSERT",
    ]


def test_create_simulation_rejects_parent_provenance_mismatch() -> None:
    parent = _record()
    child = _simulation_record(SimulationRole.BASELINE).model_copy(
        update={"worker_version": "5.3.0"}
    )
    cursor = FakeCursor([parent.model_dump(mode="python")])
    store = PostgresEvaluationStore(
        "postgresql://runtime",
        connect=lambda *_, **__: FakeConnection(cursor),
    )

    with pytest.raises(ValueError, match="worker_version"):
        store.create_or_resolve_simulation(child)

    assert len(cursor.calls) == 1


def test_successful_records_cannot_be_overwritten() -> None:
    report = _record().model_copy(
        update={
            "status": EvaluationLifecycleStatus.SUCCEEDED,
            "aggregation_status": EvaluationAggregationStatus.SUCCEEDED,
            "aggregate_output_uri": "gs://private/report.json",
            "aggregate_output_sha256": "a" * 64,
            "aggregate_schema_version": 1,
            "completed_at": NOW,
        }
    )
    report_cursor = FakeCursor([report.model_dump(mode="python")])
    report_store = PostgresEvaluationStore(
        "postgresql://runtime",
        connect=lambda *_, **__: FakeConnection(report_cursor),
    )
    simulation = _simulation_record(SimulationRole.BASELINE).model_copy(
        update={
            "status": EvaluationLifecycleStatus.SUCCEEDED,
            "output_uri": "gs://private/baseline.parquet",
            "output_sha256": "a" * 64,
            "output_schema_version": 1,
            "row_identity_columns": ("household.household_id",),
            "row_count": 100,
            "row_identity_sha256": "b" * 64,
            "completed_at": NOW,
        }
    )
    simulation_cursor = FakeCursor([simulation.model_dump(mode="python")])
    simulation_store = PostgresEvaluationStore(
        "postgresql://runtime",
        connect=lambda *_, **__: FakeConnection(simulation_cursor),
    )

    with pytest.raises(ValueError, match="aggregate_output_sha256"):
        report_store.replace_report(
            report.model_copy(update={"aggregate_output_sha256": "b" * 64})
        )
    with pytest.raises(ValueError, match="output_sha256"):
        simulation_store.replace_simulation(
            simulation.model_copy(update={"output_sha256": "c" * 64})
        )

    assert len(report_cursor.calls) == 1
    assert len(simulation_cursor.calls) == 1


def test_retention_selects_a_bounded_batch_and_deletes_by_cutoff() -> None:
    record = _record()
    cutoff = NOW + timedelta(days=31)
    select_cursor = FakeCursor([record.model_dump(mode="python")])
    delete_cursor = FakeCursor([{"evaluation_id": record.evaluation_id}])
    cursors = iter((select_cursor, delete_cursor))
    store = PostgresEvaluationStore(
        "postgresql://runtime",
        connect=lambda *_, **__: FakeConnection(next(cursors)),
    )

    selected = store.list_expired_reports(expired_before=cutoff, limit=100)
    deleted = store.delete_expired_report(
        record.evaluation_id,
        expired_before=cutoff,
    )

    assert selected == (record,)
    assert deleted is True
    assert select_cursor.calls[0][1]["limit"] == 100
    assert delete_cursor.calls[0][0].startswith("DELETE FROM")


def test_attaching_report_invocation_preserves_completed_lifecycle() -> None:
    record = _record().model_copy(
        update={
            "status": EvaluationLifecycleStatus.SUCCEEDED,
            "aggregation_status": EvaluationAggregationStatus.SUCCEEDED,
            "coordinator_invocation_id": "modal-call-1",
            "aggregate_output_uri": "gs://private/report.json",
            "aggregate_output_sha256": "a" * 64,
            "aggregate_schema_version": 1,
            "completed_at": NOW,
        }
    )
    cursor = FakeCursor([record.model_dump(mode="python")])
    store = PostgresEvaluationStore(
        "postgresql://runtime",
        connect=lambda *_, **__: FakeConnection(cursor),
    )

    resolved = store.attach_report_invocation(
        record.evaluation_id,
        expected_placeholder="dispatch-pending-1",
        modal_invocation_id="modal-call-1",
        updated_at=NOW,
    )

    assert resolved.status is EvaluationLifecycleStatus.SUCCEEDED
    assert resolved.coordinator_invocation_id == "modal-call-1"
    assert "coordinator_invocation_id = %(modal_invocation_id)s" in cursor.calls[0][0]
    assert "status =" not in cursor.calls[0][0]


def test_list_simulations_reads_children_in_database_order() -> None:
    baseline = _simulation_record(SimulationRole.BASELINE)
    reform = _simulation_record(SimulationRole.REFORM)
    cursor = FakeCursor(
        [
            baseline.model_dump(mode="python"),
            reform.model_dump(mode="python"),
        ]
    )
    store = PostgresEvaluationStore(
        "postgresql://runtime",
        connect=lambda *_, **__: FakeConnection(cursor),
    )

    simulations = store.list_simulations(baseline.evaluation_id)

    assert simulations == (baseline, reform)
    statement, parameters = cursor.calls[0]
    assert "WHERE evaluation_id = %(evaluation_id)s" in statement
    assert "ORDER BY role, simulation_execution_id" in statement
    assert parameters == {"evaluation_id": baseline.evaluation_id}
