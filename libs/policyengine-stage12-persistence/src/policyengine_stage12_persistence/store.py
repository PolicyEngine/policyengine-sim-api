"""Transactional SQLAlchemy persistence for temporary Stage 12 records."""

from __future__ import annotations

from contextlib import contextmanager
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

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
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.pool import NullPool

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

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import ColumnElement, RowMapping, Table

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
        msg = "Stage 12 database URL is missing or malformed"
        raise ValueError(msg)
    try:
        parsed = make_url(database_url)
    except Exception as error:
        msg = "Stage 12 database URL is malformed"
        raise ValueError(msg) from error
    if parsed.drivername != "postgresql":
        msg = "Stage 12 database URL must use postgresql://"
        raise ValueError(msg)
    if not parsed.username or not parsed.host or not parsed.database:
        msg = "Stage 12 database URL is incomplete"
        raise ValueError(msg)
    return parsed.set(drivername="postgresql+psycopg")


def create_stage12_engine(database_url: str) -> Engine:
    """Build the DML-only runtime engine; this never creates database objects."""
    return create_engine(
        normalized_database_url(database_url),
        # Coordinators and workers spend most of their lifetime calculating
        # or waiting. Release each database session after its short operation
        # instead of reserving a pooler slot throughout that idle interval.
        poolclass=NullPool,
        connect_args={"connect_timeout": 10},
    )


class ConnectionOpener(Protocol):
    """Open the database connection that one store operation uses.

    ``connect`` only opens a connection; it sends no application statement.
    The operation's statements, and the COMMIT or ROLLBACK that ends it, run
    after the opener returns. An opener may therefore retry ``connect``
    without risking a duplicate write, and never sees a later failure.
    """

    def __call__(
        self,
        connect: Callable[[], Connection],
        *,
        operation_name: str,
    ) -> Connection:
        """Return the connection ``connect`` opens for ``operation_name``."""
        ...


def open_connection_once(
    connect: Callable[[], Connection],
    *,
    operation_name: str,
) -> Connection:
    """Open a connection once, so a refused connection fails the operation."""
    return connect()


def _database_value(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    return value


def _record_values(record: object) -> dict[str, Any]:
    dumped = record.model_dump(mode="python")  # type: ignore[attr-defined]
    return {name: _database_value(value) for name, value in dumped.items()}


def _report(row: RowMapping) -> ComparisonReportRecord:
    return ComparisonReportRecord.model_validate(dict(row))


def _simulation(row: RowMapping) -> ComparisonSimulationRecord:
    return ComparisonSimulationRecord.model_validate(dict(row))


def _identity_predicates(
    table: Table, values: dict[str, Any], columns: tuple[str, ...]
) -> tuple[ColumnElement[bool], ...]:
    return tuple(table.c[name] == values[name] for name in columns)


class Stage12PersistenceStore:
    """DML-only store for the two API-owned temporary comparison tables."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        open_connection: ConnectionOpener = open_connection_once,
    ) -> None:
        """Use ``engine`` if given, else build one from ``database_url``.

        Raises ValueError when neither is given or the URL is invalid.
        """
        if engine is None:
            if database_url is None:
                msg = "Stage 12 database URL is required"
                raise ValueError(msg)
            engine = create_stage12_engine(database_url)
        self.engine = engine
        self._open_connection = open_connection

    @contextmanager
    def _connection(self, operation_name: str) -> Iterator[Connection]:
        # Opening the connection is the only step the opener sees. Every
        # statement, COMMIT and ROLLBACK runs after the opener returns, so
        # an opener that retries can never repeat work the database received.
        connection = self._open_connection(
            self.engine.connect,
            operation_name=operation_name,
        )
        with connection:
            yield connection

    @contextmanager
    def _transaction(self, operation_name: str) -> Iterator[Connection]:
        with self._connection(operation_name) as connection, connection.begin():
            yield connection

    def create_or_resolve_report(
        self,
        record: ComparisonReportRecord,
    ) -> ComparisonReportPersistenceResult:
        """Insert the report, or return the stored one with the same identity.

        ``created`` on the result says which happened. A stored report must match
        ``record``'s provenance, else ValueError.
        """
        values = _record_values(record)
        statement = (
            insert(comparison_reports)
            .values(**values)
            .on_conflict_do_nothing(constraint=REPORT_IDENTITY_CONSTRAINT)
            .returning(*comparison_reports.c)
        )
        with self._transaction("create_or_resolve_report") as connection:
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
                msg = (
                    "comparison report conflict did not resolve to an existing identity"
                )
                raise ValueError(msg)
            resolved = _report(existing)
            require_report_conflict_matches(resolved, record)
            return ComparisonReportPersistenceResult(record=resolved, created=False)

    def get_report(self, evaluation_id: UUID) -> ComparisonReportRecord:
        """Return the stored report; raise LookupError if there is none."""
        with self._connection("get_report") as connection:
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
            msg = f"comparison report {evaluation_id} does not exist"
            raise LookupError(msg)
        return _report(row)

    def list_simulations(
        self,
        evaluation_id: UUID,
    ) -> tuple[ComparisonSimulationRecord, ...]:
        """Return the report's simulations ordered by role, then execution ID."""
        statement = (
            select(comparison_simulations)
            .where(comparison_simulations.c.evaluation_id == evaluation_id)
            .order_by(
                comparison_simulations.c.role,
                comparison_simulations.c.simulation_execution_id,
            )
        )
        with self._connection("list_simulations") as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_simulation(row) for row in rows)

    def replace_report(
        self,
        record: ComparisonReportRecord,
    ) -> ComparisonReportRecord:
        """Write ``record``'s lifecycle and aggregation fields over the stored report.

        The row is locked for the update. Raises LookupError if the report is missing,
        and ValueError for an identity or result-comparison change or a disallowed
        status move. A succeeded report is returned as stored when ``record`` replays
        it exactly.
        """
        values = _record_values(record)
        with self._transaction("replace_report") as connection:
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
                msg = f"comparison report {record.evaluation_id} does not exist"
                raise LookupError(msg)
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
        """Write ``record``'s result-comparison fields over the stored report.

        The row is locked for the update and every other field must be unchanged.
        Once the comparison is MATCHED or DIFFERENT the stored report is returned
        as-is, provided ``record`` repeats its result. Raises LookupError if the
        report is missing and ValueError for any other change or disallowed move.
        """
        values = _record_values(record)
        with self._transaction("replace_report_result_comparison") as connection:
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
                msg = f"comparison report {record.evaluation_id} does not exist"
                raise LookupError(msg)
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
        """Insert the simulation, or return the stored one with the same identity.

        The parent report must exist (else LookupError) and share ``record``'s
        provenance; a stored simulation must match ``record`` (else ValueError).
        """
        values = _record_values(record)
        statement = (
            insert(comparison_simulations)
            .values(**values)
            .on_conflict_do_nothing(constraint=SIMULATION_IDENTITY_CONSTRAINT)
            .returning(*comparison_simulations.c)
        )
        with self._transaction("create_or_resolve_simulation") as connection:
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
                msg = f"comparison report {record.evaluation_id} does not exist"
                raise LookupError(msg)
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
                msg = "comparison simulation conflict did not resolve to an existing identity"
                raise ValueError(msg)
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
        """Return the stored simulation; raise LookupError if there is none."""
        with self._connection("get_simulation") as connection:
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
            msg = f"comparison simulation {simulation_execution_id} does not exist"
            raise LookupError(msg)
        return _simulation(row)

    def replace_simulation(
        self,
        record: ComparisonSimulationRecord,
    ) -> ComparisonSimulationRecord:
        """Write ``record``'s mutable fields over the stored simulation.

        The row is locked for the update. Raises LookupError if the simulation is
        missing, and ValueError for an identity change or a disallowed status move. A
        succeeded simulation is returned as stored when ``record`` replays it exactly.
        """
        values = _record_values(record)
        with self._transaction("replace_simulation") as connection:
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
                msg = (
                    "comparison simulation "
                    f"{record.simulation_execution_id} does not exist"
                )
                raise LookupError(msg)
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
        """Replace the placeholder invocation ID with the real Modal invocation ID.

        The update applies only while ``expected_placeholder`` is still stored, so
        repeating an attach that already succeeded returns the stored record. Raises
        LookupError if the simulation is missing and ValueError if a different
        invocation ID is already attached.
        """
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
        with self._transaction("attach_simulation_invocation") as connection:
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
                msg = f"comparison simulation {simulation_execution_id} does not exist"
                raise LookupError(msg)
            resolved = _simulation(row)
            if resolved.modal_invocation_id != modal_invocation_id:
                msg = "comparison simulation invocation identity changed"
                raise ValueError(msg)
            return resolved
