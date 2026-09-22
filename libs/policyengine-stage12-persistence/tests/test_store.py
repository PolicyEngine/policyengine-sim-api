from __future__ import annotations

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
from policyengine_stage12_persistence.store import normalized_database_url
from sqlalchemy.sql import ClauseElement

NOW = datetime(2026, 9, 22, tzinfo=UTC)
REPORT_ID = UUID("00000000-0000-0000-0000-000000000001")
SIMULATION_ID = UUID("00000000-0000-0000-0000-000000000002")


def _report() -> ComparisonReportRecord:
    return ComparisonReportRecord(
        evaluation_id=REPORT_ID,
        status=ComparisonRunLifecycleStatus.PENDING,
        aggregation_status=ComparisonRunAggregationStatus.NOT_STARTED,
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


class FakeConnection:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    def execute(self, statement):
        assert isinstance(statement, ClauseElement)
        self.statements.append(statement)
        return self.results.pop(0)


class FakeEngine:
    def __init__(self, results):
        self.connection = FakeConnection(results)

    @contextmanager
    def begin(self):
        yield self.connection

    @contextmanager
    def connect(self):
        yield self.connection


def test_create_and_read_report_use_sqlalchemy_statements_only() -> None:
    report = _report()
    engine = FakeEngine([FakeResult(row=_row(report))])
    store = Stage12PersistenceStore(engine=engine)  # type: ignore[arg-type]

    result = store.create_or_resolve_report(report)

    assert result.created is True
    assert result.record == report
    assert len(engine.connection.statements) == 1


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
