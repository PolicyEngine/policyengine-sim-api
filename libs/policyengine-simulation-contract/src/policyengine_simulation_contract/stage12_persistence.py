"""DML-only Postgres adapter for temporary Stage 12 evaluation records.

PolicyEngine/policyengine-api owns the SQLModel definitions and Alembic chain.
This adapter consumes that versioned contract and never creates or alters a
database object.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from policyengine_simulation_contract.stage12_execution import (
    EvaluationAggregationStatus,
    EvaluationLifecycleStatus,
    EvaluationReportPersistenceResult,
    EvaluationReportRecord,
    EvaluationSimulationPersistenceResult,
    EvaluationSimulationRecord,
)

REPORT_TABLE = "public.stage12_evaluation_reports"
SIMULATION_TABLE = "public.stage12_evaluation_simulations"
REPORT_CONSTRAINT = "uq_stage12_eval_reports_identity"
SIMULATION_CONSTRAINT = "uq_stage12_eval_simulations_identity"

REPORT_COLUMNS = tuple(EvaluationReportRecord.model_fields)
SIMULATION_COLUMNS = tuple(EvaluationSimulationRecord.model_fields)
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

ALLOWED_LIFECYCLE_TRANSITIONS = {
    EvaluationLifecycleStatus.PENDING: frozenset(
        {
            EvaluationLifecycleStatus.PENDING,
            EvaluationLifecycleStatus.RUNNING,
            EvaluationLifecycleStatus.FAILED,
            EvaluationLifecycleStatus.SKIPPED,
        }
    ),
    EvaluationLifecycleStatus.RUNNING: frozenset(
        {
            EvaluationLifecycleStatus.RUNNING,
            EvaluationLifecycleStatus.SUCCEEDED,
            EvaluationLifecycleStatus.FAILED,
            EvaluationLifecycleStatus.INCOMPLETE,
        }
    ),
    EvaluationLifecycleStatus.INCOMPLETE: frozenset(
        {
            EvaluationLifecycleStatus.INCOMPLETE,
            EvaluationLifecycleStatus.RUNNING,
            EvaluationLifecycleStatus.FAILED,
        }
    ),
    EvaluationLifecycleStatus.SUCCEEDED: frozenset(
        {EvaluationLifecycleStatus.SUCCEEDED}
    ),
    EvaluationLifecycleStatus.FAILED: frozenset(
        {
            EvaluationLifecycleStatus.FAILED,
            EvaluationLifecycleStatus.RUNNING,
        }
    ),
    EvaluationLifecycleStatus.SKIPPED: frozenset({EvaluationLifecycleStatus.SKIPPED}),
}
ALLOWED_AGGREGATION_TRANSITIONS = {
    EvaluationAggregationStatus.NOT_STARTED: frozenset(
        {
            EvaluationAggregationStatus.NOT_STARTED,
            EvaluationAggregationStatus.RUNNING,
            EvaluationAggregationStatus.FAILED,
        }
    ),
    EvaluationAggregationStatus.RUNNING: frozenset(
        {
            EvaluationAggregationStatus.RUNNING,
            EvaluationAggregationStatus.SUCCEEDED,
            EvaluationAggregationStatus.FAILED,
        }
    ),
    EvaluationAggregationStatus.SUCCEEDED: frozenset(
        {EvaluationAggregationStatus.SUCCEEDED}
    ),
    EvaluationAggregationStatus.FAILED: frozenset(
        {
            EvaluationAggregationStatus.FAILED,
            EvaluationAggregationStatus.RUNNING,
        }
    ),
}


def _column_list(columns: Iterable[str]) -> str:
    return ", ".join(columns)


def _placeholders(columns: Iterable[str]) -> str:
    return ", ".join(f"%({column})s" for column in columns)


def _where(columns: Iterable[str]) -> str:
    return " AND ".join(f"{column} = %({column})s" for column in columns)


def _insert_sql(table: str, constraint: str, columns: tuple[str, ...]) -> str:
    return (
        f"INSERT INTO {table} ({_column_list(columns)}) "
        f"VALUES ({_placeholders(columns)}) "
        f"ON CONFLICT ON CONSTRAINT {constraint} DO NOTHING "
        f"RETURNING {_column_list(columns)}"
    )


def _select_identity_sql(
    table: str,
    columns: tuple[str, ...],
    identity_columns: tuple[str, ...],
) -> str:
    return (
        f"SELECT {_column_list(columns)} FROM {table} WHERE {_where(identity_columns)}"
    )


def _select_id_sql(
    table: str,
    columns: tuple[str, ...],
    id_column: str,
    *,
    lock: bool = False,
) -> str:
    suffix = " FOR UPDATE" if lock else ""
    return (
        f"SELECT {_column_list(columns)} FROM {table} "
        f"WHERE {id_column} = %({id_column})s{suffix}"
    )


def _update_sql(
    table: str,
    columns: tuple[str, ...],
    mutable_columns: tuple[str, ...],
    id_column: str,
) -> str:
    assignments = ", ".join(f"{column} = %({column})s" for column in mutable_columns)
    return (
        f"UPDATE {table} SET {assignments} "
        f"WHERE {id_column} = %({id_column})s "
        f"RETURNING {_column_list(columns)}"
    )


REPORT_INSERT_SQL = _insert_sql(REPORT_TABLE, REPORT_CONSTRAINT, REPORT_COLUMNS)
SIMULATION_INSERT_SQL = _insert_sql(
    SIMULATION_TABLE,
    SIMULATION_CONSTRAINT,
    SIMULATION_COLUMNS,
)
REPORT_SELECT_IDENTITY_SQL = _select_identity_sql(
    REPORT_TABLE,
    REPORT_COLUMNS,
    REPORT_IDENTITY_COLUMNS,
)
SIMULATION_SELECT_IDENTITY_SQL = _select_identity_sql(
    SIMULATION_TABLE,
    SIMULATION_COLUMNS,
    SIMULATION_IDENTITY_COLUMNS,
)
REPORT_SELECT_ID_SQL = _select_id_sql(
    REPORT_TABLE,
    REPORT_COLUMNS,
    "evaluation_id",
)
REPORT_SELECT_PRODUCTION_SQL = (
    f"SELECT {_column_list(REPORT_COLUMNS)} FROM {REPORT_TABLE} "
    "WHERE environment = %(environment)s "
    "AND calculation_flow = %(calculation_flow)s "
    "AND production_identity = %(production_identity)s "
    "ORDER BY created_at DESC LIMIT 1"
)
SIMULATION_SELECT_ID_SQL = _select_id_sql(
    SIMULATION_TABLE,
    SIMULATION_COLUMNS,
    "simulation_execution_id",
)
SIMULATIONS_SELECT_REPORT_SQL = f"""
SELECT {", ".join(SIMULATION_COLUMNS)}
FROM {SIMULATION_TABLE}
WHERE evaluation_id = %(evaluation_id)s
ORDER BY role, simulation_execution_id
""".strip()
REPORT_SELECT_ID_FOR_UPDATE_SQL = _select_id_sql(
    REPORT_TABLE,
    REPORT_COLUMNS,
    "evaluation_id",
    lock=True,
)
SIMULATION_SELECT_ID_FOR_UPDATE_SQL = _select_id_sql(
    SIMULATION_TABLE,
    SIMULATION_COLUMNS,
    "simulation_execution_id",
    lock=True,
)
REPORT_UPDATE_SQL = _update_sql(
    REPORT_TABLE,
    REPORT_COLUMNS,
    REPORT_MUTABLE_COLUMNS,
    "evaluation_id",
)
SIMULATION_UPDATE_SQL = _update_sql(
    SIMULATION_TABLE,
    SIMULATION_COLUMNS,
    SIMULATION_MUTABLE_COLUMNS,
    "simulation_execution_id",
)
REPORT_ATTACH_INVOCATION_SQL = f"""
UPDATE {REPORT_TABLE}
SET coordinator_invocation_id = %(modal_invocation_id)s,
    updated_at = %(updated_at)s
WHERE evaluation_id = %(evaluation_id)s
  AND coordinator_invocation_id = %(expected_placeholder)s
RETURNING {", ".join(REPORT_COLUMNS)}
""".strip()
SIMULATION_ATTACH_INVOCATION_SQL = f"""
UPDATE {SIMULATION_TABLE}
SET modal_invocation_id = %(modal_invocation_id)s,
    updated_at = %(updated_at)s
WHERE simulation_execution_id = %(simulation_execution_id)s
  AND modal_invocation_id = %(expected_placeholder)s
RETURNING {", ".join(SIMULATION_COLUMNS)}
""".strip()
REPORT_SELECT_EXPIRED_SQL = f"""
SELECT {", ".join(REPORT_COLUMNS)}
FROM {REPORT_TABLE}
WHERE retention_expires_at <= %(expired_before)s
ORDER BY retention_expires_at, evaluation_id
LIMIT %(limit)s
""".strip()
REPORT_DELETE_EXPIRED_SQL = f"""
DELETE FROM {REPORT_TABLE}
WHERE evaluation_id = %(evaluation_id)s
  AND retention_expires_at <= %(expired_before)s
RETURNING evaluation_id
""".strip()


def _parameters(record: EvaluationReportRecord | EvaluationSimulationRecord) -> dict:
    values = record.model_dump(mode="python")
    for key, value in tuple(values.items()):
        if hasattr(value, "value"):
            values[key] = value.value
    if "row_identity_columns" in values and values["row_identity_columns"] is not None:
        values["row_identity_columns"] = Jsonb(list(values["row_identity_columns"]))
    return values


def _require_same(existing: object, candidate: object, columns: Iterable[str]) -> None:
    for column in columns:
        if getattr(existing, column) != getattr(candidate, column):
            raise ValueError(f"evaluation field {column} is immutable")


def _require_child_matches_parent(
    parent: EvaluationReportRecord,
    child: EvaluationSimulationRecord,
) -> None:
    if child.evaluation_id != parent.evaluation_id:
        raise ValueError("evaluation child names another parent")
    for column in (
        "contract_version",
        "worker_version",
        "modal_application",
        "version_manifest_sha256",
        "created_at",
        "retention_expires_at",
    ):
        if getattr(child, column) != getattr(parent, column):
            raise ValueError(f"evaluation child {column} does not match its parent")


def _require_successful_replay(
    existing: EvaluationReportRecord | EvaluationSimulationRecord,
    candidate: EvaluationReportRecord | EvaluationSimulationRecord,
    columns: tuple[str, ...],
) -> None:
    _require_same(
        existing,
        candidate,
        (column for column in columns if column != "updated_at"),
    )


class PostgresEvaluationStore:
    """Write temporary records with no schema-migration capability."""

    def __init__(
        self,
        database_url: str,
        *,
        connect: Callable[..., Any] = psycopg.connect,
    ) -> None:
        if not database_url:
            raise ValueError("Stage 12 database URL is required")
        self._database_url = database_url
        self._connect = connect

    def _connection(self):
        return self._connect(self._database_url, row_factory=dict_row)

    def create_or_resolve_report(
        self,
        record: EvaluationReportRecord,
    ) -> EvaluationReportPersistenceResult:
        values = _parameters(record)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(REPORT_INSERT_SQL, values)
            row = cursor.fetchone()
            created = row is not None
            if row is None:
                cursor.execute(REPORT_SELECT_IDENTITY_SQL, values)
                row = cursor.fetchone()
            if row is None:
                raise RuntimeError("report conflict did not resolve to a record")
            resolved = EvaluationReportRecord.model_validate(row)
            _require_same(resolved, record, REPORT_IDENTITY_COLUMNS)
            return EvaluationReportPersistenceResult(record=resolved, created=created)

    def create_or_resolve_simulation(
        self,
        record: EvaluationSimulationRecord,
    ) -> EvaluationSimulationPersistenceResult:
        values = _parameters(record)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                REPORT_SELECT_ID_SQL,
                {"evaluation_id": record.evaluation_id},
            )
            parent_row = cursor.fetchone()
            if parent_row is None:
                raise LookupError(
                    f"evaluation report {record.evaluation_id} does not exist"
                )
            _require_child_matches_parent(
                EvaluationReportRecord.model_validate(parent_row),
                record,
            )
            cursor.execute(SIMULATION_INSERT_SQL, values)
            row = cursor.fetchone()
            created = row is not None
            if row is None:
                cursor.execute(SIMULATION_SELECT_IDENTITY_SQL, values)
                row = cursor.fetchone()
            if row is None:
                raise RuntimeError("simulation conflict did not resolve to a record")
            resolved = EvaluationSimulationRecord.model_validate(row)
            _require_same(resolved, record, SIMULATION_IDENTITY_COLUMNS)
            return EvaluationSimulationPersistenceResult(
                record=resolved,
                created=created,
            )

    def get_report(self, evaluation_id: UUID) -> EvaluationReportRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(REPORT_SELECT_ID_SQL, {"evaluation_id": evaluation_id})
            row = cursor.fetchone()
        if row is None:
            raise LookupError(f"evaluation report {evaluation_id} does not exist")
        return EvaluationReportRecord.model_validate(row)

    def get_report_for_production(
        self,
        *,
        environment: str,
        calculation_flow: str,
        production_identity: str,
    ) -> EvaluationReportRecord | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                REPORT_SELECT_PRODUCTION_SQL,
                {
                    "environment": environment,
                    "calculation_flow": calculation_flow,
                    "production_identity": production_identity,
                },
            )
            row = cursor.fetchone()
        return EvaluationReportRecord.model_validate(row) if row is not None else None

    def get_simulation(
        self,
        simulation_execution_id: UUID,
    ) -> EvaluationSimulationRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                SIMULATION_SELECT_ID_SQL,
                {"simulation_execution_id": simulation_execution_id},
            )
            row = cursor.fetchone()
        if row is None:
            raise LookupError(
                f"evaluation simulation {simulation_execution_id} does not exist"
            )
        return EvaluationSimulationRecord.model_validate(row)

    def list_simulations(
        self,
        evaluation_id: UUID,
    ) -> tuple[EvaluationSimulationRecord, ...]:
        """Return the temporary child executions for one Stage 12 report."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                SIMULATIONS_SELECT_REPORT_SQL,
                {"evaluation_id": evaluation_id},
            )
            rows = cursor.fetchall()
        return tuple(EvaluationSimulationRecord.model_validate(row) for row in rows)

    def replace_report(
        self,
        record: EvaluationReportRecord,
    ) -> EvaluationReportRecord:
        values = _parameters(record)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(REPORT_SELECT_ID_FOR_UPDATE_SQL, values)
            row = cursor.fetchone()
            if row is None:
                raise LookupError(
                    f"evaluation report {record.evaluation_id} does not exist"
                )
            existing = EvaluationReportRecord.model_validate(row)
            _require_same(
                existing,
                record,
                tuple(
                    column
                    for column in REPORT_COLUMNS
                    if column not in REPORT_MUTABLE_COLUMNS
                ),
            )
            if record.status not in ALLOWED_LIFECYCLE_TRANSITIONS[existing.status]:
                raise ValueError("invalid report lifecycle transition")
            if (
                record.aggregation_status
                not in ALLOWED_AGGREGATION_TRANSITIONS[existing.aggregation_status]
            ):
                raise ValueError("invalid report aggregation transition")
            if existing.status is EvaluationLifecycleStatus.SUCCEEDED:
                _require_successful_replay(existing, record, REPORT_COLUMNS)
                return existing
            cursor.execute(REPORT_UPDATE_SQL, values)
            updated = cursor.fetchone()
            if updated is None:  # pragma: no cover - row remains locked
                raise RuntimeError("report update returned no record")
            return EvaluationReportRecord.model_validate(updated)

    def replace_simulation(
        self,
        record: EvaluationSimulationRecord,
    ) -> EvaluationSimulationRecord:
        values = _parameters(record)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(SIMULATION_SELECT_ID_FOR_UPDATE_SQL, values)
            row = cursor.fetchone()
            if row is None:
                raise LookupError(
                    f"evaluation simulation {record.simulation_execution_id} does not exist"
                )
            existing = EvaluationSimulationRecord.model_validate(row)
            _require_same(
                existing,
                record,
                tuple(
                    column
                    for column in SIMULATION_COLUMNS
                    if column not in SIMULATION_MUTABLE_COLUMNS
                ),
            )
            if record.status not in ALLOWED_LIFECYCLE_TRANSITIONS[existing.status]:
                raise ValueError("invalid simulation lifecycle transition")
            if existing.status is EvaluationLifecycleStatus.SUCCEEDED:
                _require_successful_replay(existing, record, SIMULATION_COLUMNS)
                return existing
            cursor.execute(SIMULATION_UPDATE_SQL, values)
            updated = cursor.fetchone()
            if updated is None:  # pragma: no cover - row remains locked
                raise RuntimeError("simulation update returned no record")
            return EvaluationSimulationRecord.model_validate(updated)

    def attach_report_invocation(
        self,
        evaluation_id: UUID,
        *,
        expected_placeholder: str,
        modal_invocation_id: str,
        updated_at: datetime,
    ) -> EvaluationReportRecord:
        parameters = {
            "evaluation_id": evaluation_id,
            "expected_placeholder": expected_placeholder,
            "modal_invocation_id": modal_invocation_id,
            "updated_at": updated_at,
        }
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(REPORT_ATTACH_INVOCATION_SQL, parameters)
            row = cursor.fetchone()
            if row is None:
                cursor.execute(REPORT_SELECT_ID_SQL, parameters)
                row = cursor.fetchone()
        if row is None:
            raise LookupError(f"evaluation report {evaluation_id} does not exist")
        resolved = EvaluationReportRecord.model_validate(row)
        if resolved.coordinator_invocation_id != modal_invocation_id:
            raise ValueError("evaluation report invocation identity changed")
        return resolved

    def attach_simulation_invocation(
        self,
        simulation_execution_id: UUID,
        *,
        expected_placeholder: str,
        modal_invocation_id: str,
        updated_at: datetime,
    ) -> EvaluationSimulationRecord:
        parameters = {
            "simulation_execution_id": simulation_execution_id,
            "expected_placeholder": expected_placeholder,
            "modal_invocation_id": modal_invocation_id,
            "updated_at": updated_at,
        }
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(SIMULATION_ATTACH_INVOCATION_SQL, parameters)
            row = cursor.fetchone()
            if row is None:
                cursor.execute(SIMULATION_SELECT_ID_SQL, parameters)
                row = cursor.fetchone()
        if row is None:
            raise LookupError(
                f"evaluation simulation {simulation_execution_id} does not exist"
            )
        resolved = EvaluationSimulationRecord.model_validate(row)
        if resolved.modal_invocation_id != modal_invocation_id:
            raise ValueError("evaluation simulation invocation identity changed")
        return resolved

    def list_expired_reports(
        self,
        *,
        expired_before: datetime,
        limit: int,
    ) -> tuple[EvaluationReportRecord, ...]:
        if expired_before.tzinfo is None:
            raise ValueError("retention cutoff must include a timezone")
        if limit < 1 or limit > 1_000:
            raise ValueError("retention batch limit must be between 1 and 1000")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                REPORT_SELECT_EXPIRED_SQL,
                {"expired_before": expired_before, "limit": limit},
            )
            rows = cursor.fetchall()
        return tuple(EvaluationReportRecord.model_validate(row) for row in rows)

    def delete_expired_report(
        self,
        evaluation_id: UUID,
        *,
        expired_before: datetime,
    ) -> bool:
        if expired_before.tzinfo is None:
            raise ValueError("retention cutoff must include a timezone")
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                REPORT_DELETE_EXPIRED_SQL,
                {
                    "evaluation_id": evaluation_id,
                    "expired_before": expired_before,
                },
            )
            return cursor.fetchone() is not None


def sql_statements() -> Mapping[str, str]:
    """Expose bounded DML for tests and operational review."""

    return {
        "report_insert": REPORT_INSERT_SQL,
        "simulation_insert": SIMULATION_INSERT_SQL,
        "report_select_identity": REPORT_SELECT_IDENTITY_SQL,
        "simulation_select_identity": SIMULATION_SELECT_IDENTITY_SQL,
        "report_select_id": REPORT_SELECT_ID_SQL,
        "report_select_production": REPORT_SELECT_PRODUCTION_SQL,
        "simulation_select_id": SIMULATION_SELECT_ID_SQL,
        "simulations_select_report": SIMULATIONS_SELECT_REPORT_SQL,
        "report_update": REPORT_UPDATE_SQL,
        "simulation_update": SIMULATION_UPDATE_SQL,
        "report_attach_invocation": REPORT_ATTACH_INVOCATION_SQL,
        "simulation_attach_invocation": SIMULATION_ATTACH_INVOCATION_SQL,
        "report_select_expired": REPORT_SELECT_EXPIRED_SQL,
        "report_delete_expired": REPORT_DELETE_EXPIRED_SQL,
    }
