"""Transactional SQLAlchemy persistence for temporary Stage 12 records."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportPersistenceResult,
    ComparisonReportRecord,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationPersistenceResult,
    ComparisonSimulationRecord,
    ResultComparisonStatus,
)
from sqlalchemy import Engine, create_engine, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import URL, make_url

from policyengine_stage12_persistence.tables import (
    REPORT_IDENTITY_CONSTRAINT,
    SIMULATION_IDENTITY_CONSTRAINT,
    comparison_reports,
    comparison_simulations,
)
from policyengine_stage12_persistence.validators import (
    require_aggregation_transition,
    require_comparison_update_only,
    require_lifecycle_transition,
    require_report_conflict_matches,
    require_report_identity,
    require_result_comparison_transition,
    require_result_comparison_unchanged,
    require_simulation_conflict_matches,
    require_simulation_identity,
    require_simulation_parent_matches,
    require_successful_report_replay,
    require_successful_simulation_replay,
)

REPORT_IDENTITY_COLUMNS = (
    "environment",
    "calculation_flow",
    "production_identity",
    "worker_version",
    "version_manifest_sha256",
)
SIMULATION_IDENTITY_COLUMNS = (
    "evaluation_id",
    "role",
    "input_sha256",
    "worker_version",
    "version_manifest_sha256",
)
REPORT_MUTABLE_COLUMNS = (
    "status",
    "aggregation_status",
    "coordinator_invocation_id",
    "error_code",
    "error_summary",
    "aggregate_output_uri",
    "aggregate_output_sha256",
    "aggregate_schema_version",
    "updated_at",
    "started_at",
    "completed_at",
)
REPORT_COMPARISON_COLUMNS = (
    "comparison_status",
    "comparison_output_uri",
    "comparison_output_sha256",
    "comparison_schema_version",
    "comparison_completed_at",
    "comparison_error_code",
    "comparison_error_summary",
    "updated_at",
)
SIMULATION_MUTABLE_COLUMNS = (
    "status",
    "modal_invocation_id",
    "error_code",
    "error_summary",
    "output_uri",
    "output_sha256",
    "output_schema_version",
    "row_identity_columns",
    "row_count",
    "row_identity_sha256",
    "updated_at",
    "started_at",
    "completed_at",
)


def normalized_database_url(database_url: str) -> URL:
    """Select psycopg 3 without changing the stored secret's URL format."""

    if not database_url or database_url != database_url.strip():
        raise ValueError("Stage 12 database URL is missing or malformed")
    try:
        parsed = make_url(database_url)
    except Exception as error:
        raise ValueError("Stage 12 database URL is malformed") from error
    if parsed.drivername != "postgresql":
        raise ValueError("Stage 12 database URL must use postgresql://")
    if not parsed.username or not parsed.host or not parsed.database:
        raise ValueError("Stage 12 database URL is incomplete")
    return parsed.set(drivername="postgresql+psycopg")


def create_stage12_engine(database_url: str) -> Engine:
    """Build the DML-only runtime engine; this never creates database objects."""

    return create_engine(
        normalized_database_url(database_url),
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=5,
        connect_args={"connect_timeout": 10},
    )


def _database_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    return value


def _record_values(record: object) -> dict[str, Any]:
    dumped = record.model_dump(mode="python")  # type: ignore[attr-defined]
    return {name: _database_value(value) for name, value in dumped.items()}


def _report(row: Any) -> ComparisonReportRecord:
    return ComparisonReportRecord.model_validate(dict(row))


def _simulation(row: Any) -> ComparisonSimulationRecord:
    return ComparisonSimulationRecord.model_validate(dict(row))


def _identity_predicates(table: Any, values: dict[str, Any], columns: tuple[str, ...]):
    return tuple(table.c[name] == values[name] for name in columns)


class Stage12PersistenceStore:
    """DML-only store for the two API-owned temporary comparison tables."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        if engine is None:
            if database_url is None:
                raise ValueError("Stage 12 database URL is required")
            engine = create_stage12_engine(database_url)
        self.engine = engine

    def create_or_resolve_report(
        self,
        record: ComparisonReportRecord,
    ) -> ComparisonReportPersistenceResult:
        values = _record_values(record)
        statement = (
            insert(comparison_reports)
            .values(**values)
            .on_conflict_do_nothing(constraint=REPORT_IDENTITY_CONSTRAINT)
            .returning(*comparison_reports.c)
        )
        with self.engine.begin() as connection:
            created = connection.execute(statement).mappings().one_or_none()
            if created is not None:
                return ComparisonReportPersistenceResult(
                    record=_report(created),
                    created=True,
                )
            existing = (
                connection.execute(
                    select(comparison_reports).where(
                        *_identity_predicates(
                            comparison_reports,
                            values,
                            REPORT_IDENTITY_COLUMNS,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                raise ValueError(
                    "comparison report conflict did not resolve to an existing identity"
                )
            resolved = _report(existing)
            require_report_conflict_matches(resolved, record)
            return ComparisonReportPersistenceResult(record=resolved, created=False)

    def get_report(self, evaluation_id: UUID) -> ComparisonReportRecord:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(comparison_reports).where(
                        comparison_reports.c.evaluation_id == evaluation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise LookupError(f"comparison report {evaluation_id} does not exist")
        return _report(row)

    def list_simulations(
        self,
        evaluation_id: UUID,
    ) -> tuple[ComparisonSimulationRecord, ...]:
        statement = (
            select(comparison_simulations)
            .where(comparison_simulations.c.evaluation_id == evaluation_id)
            .order_by(
                comparison_simulations.c.role,
                comparison_simulations.c.simulation_execution_id,
            )
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_simulation(row) for row in rows)

    def replace_report(
        self,
        record: ComparisonReportRecord,
    ) -> ComparisonReportRecord:
        values = _record_values(record)
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(comparison_reports)
                    .where(comparison_reports.c.evaluation_id == record.evaluation_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise LookupError(
                    f"comparison report {record.evaluation_id} does not exist"
                )
            existing = _report(row)
            require_report_identity(existing, record)
            require_result_comparison_unchanged(existing, record)
            require_lifecycle_transition(existing.status, record.status)
            require_aggregation_transition(
                existing.aggregation_status,
                record.aggregation_status,
            )
            if existing.status is ComparisonRunLifecycleStatus.SUCCEEDED:
                require_successful_report_replay(existing, record)
                return existing
            updated = (
                connection.execute(
                    update(comparison_reports)
                    .where(comparison_reports.c.evaluation_id == record.evaluation_id)
                    .values(**{name: values[name] for name in REPORT_MUTABLE_COLUMNS})
                    .returning(*comparison_reports.c)
                )
                .mappings()
                .one()
            )
            return _report(updated)

    def replace_report_result_comparison(
        self,
        record: ComparisonReportRecord,
    ) -> ComparisonReportRecord:
        values = _record_values(record)
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(comparison_reports)
                    .where(comparison_reports.c.evaluation_id == record.evaluation_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise LookupError(
                    f"comparison report {record.evaluation_id} does not exist"
                )
            existing = _report(row)
            require_comparison_update_only(existing, record)
            require_result_comparison_transition(
                existing.comparison_status,
                record.comparison_status,
            )
            if existing.comparison_status in {
                ResultComparisonStatus.MATCHED,
                ResultComparisonStatus.DIFFERENT,
            }:
                require_result_comparison_unchanged(existing, record)
                return existing
            updated = (
                connection.execute(
                    update(comparison_reports)
                    .where(comparison_reports.c.evaluation_id == record.evaluation_id)
                    .values(
                        **{name: values[name] for name in REPORT_COMPARISON_COLUMNS}
                    )
                    .returning(*comparison_reports.c)
                )
                .mappings()
                .one()
            )
            return _report(updated)

    def create_or_resolve_simulation(
        self,
        record: ComparisonSimulationRecord,
    ) -> ComparisonSimulationPersistenceResult:
        values = _record_values(record)
        statement = (
            insert(comparison_simulations)
            .values(**values)
            .on_conflict_do_nothing(constraint=SIMULATION_IDENTITY_CONSTRAINT)
            .returning(*comparison_simulations.c)
        )
        with self.engine.begin() as connection:
            parent_row = (
                connection.execute(
                    select(comparison_reports).where(
                        comparison_reports.c.evaluation_id == record.evaluation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if parent_row is None:
                raise LookupError(
                    f"comparison report {record.evaluation_id} does not exist"
                )
            require_simulation_parent_matches(_report(parent_row), record)
            created = connection.execute(statement).mappings().one_or_none()
            if created is not None:
                return ComparisonSimulationPersistenceResult(
                    record=_simulation(created),
                    created=True,
                )
            existing = (
                connection.execute(
                    select(comparison_simulations).where(
                        *_identity_predicates(
                            comparison_simulations,
                            values,
                            SIMULATION_IDENTITY_COLUMNS,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                raise ValueError(
                    "comparison simulation conflict did not resolve to an existing identity"
                )
            resolved = _simulation(existing)
            require_simulation_conflict_matches(resolved, record)
            return ComparisonSimulationPersistenceResult(
                record=resolved,
                created=False,
            )

    def get_simulation(
        self,
        simulation_execution_id: UUID,
    ) -> ComparisonSimulationRecord:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(comparison_simulations).where(
                        comparison_simulations.c.simulation_execution_id
                        == simulation_execution_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise LookupError(
                f"comparison simulation {simulation_execution_id} does not exist"
            )
        return _simulation(row)

    def replace_simulation(
        self,
        record: ComparisonSimulationRecord,
    ) -> ComparisonSimulationRecord:
        values = _record_values(record)
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(comparison_simulations)
                    .where(
                        comparison_simulations.c.simulation_execution_id
                        == record.simulation_execution_id
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise LookupError(
                    "comparison simulation "
                    f"{record.simulation_execution_id} does not exist"
                )
            existing = _simulation(row)
            require_simulation_identity(existing, record)
            require_lifecycle_transition(existing.status, record.status)
            if existing.status is ComparisonRunLifecycleStatus.SUCCEEDED:
                require_successful_simulation_replay(existing, record)
                return existing
            updated = (
                connection.execute(
                    update(comparison_simulations)
                    .where(
                        comparison_simulations.c.simulation_execution_id
                        == record.simulation_execution_id
                    )
                    .values(
                        **{name: values[name] for name in SIMULATION_MUTABLE_COLUMNS}
                    )
                    .returning(*comparison_simulations.c)
                )
                .mappings()
                .one()
            )
            return _simulation(updated)

    def attach_simulation_invocation(
        self,
        simulation_execution_id: UUID,
        *,
        expected_placeholder: str,
        modal_invocation_id: str,
        updated_at: datetime,
    ) -> ComparisonSimulationRecord:
        statement = (
            update(comparison_simulations)
            .where(
                comparison_simulations.c.simulation_execution_id
                == simulation_execution_id,
                comparison_simulations.c.modal_invocation_id == expected_placeholder,
            )
            .values(
                modal_invocation_id=modal_invocation_id,
                updated_at=updated_at,
            )
            .returning(*comparison_simulations.c)
        )
        with self.engine.begin() as connection:
            row = connection.execute(statement).mappings().one_or_none()
            if row is None:
                row = (
                    connection.execute(
                        select(comparison_simulations).where(
                            comparison_simulations.c.simulation_execution_id
                            == simulation_execution_id
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            if row is None:
                raise LookupError(
                    f"comparison simulation {simulation_execution_id} does not exist"
                )
            resolved = _simulation(row)
            if resolved.modal_invocation_id != modal_invocation_id:
                raise ValueError("comparison simulation invocation identity changed")
            return resolved
