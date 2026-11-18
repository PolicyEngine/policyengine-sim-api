"""Transactional SQLAlchemy persistence for temporary Stage 12 records."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportPersistenceResult,
    ComparisonReportRecord,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationPersistenceResult,
    ComparisonSimulationRecord,
    ResultComparisonStatus,
)
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from policyengine_stage12_persistence.engine import (
    WITHOUT_OBJECT_SYNC,
    SessionOpener,
    StoreSessions,
    open_session_once,
)
from policyengine_stage12_persistence.tables import (
    REPORT_ATTRIBUTES,
    REPORT_IDENTITY_CONSTRAINT,
    SIMULATION_ATTRIBUTES,
    SIMULATION_IDENTITY_CONSTRAINT,
    ComparisonReport,
    ComparisonSimulation,
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
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import ColumnElement, RowMapping
    from sqlalchemy.orm import Session, sessionmaker

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
    model: type[ComparisonReport | ComparisonSimulation],
    values: dict[str, Any],
    columns: tuple[str, ...],
) -> tuple[ColumnElement[bool], ...]:
    return tuple(getattr(model, name) == values[name] for name in columns)


class Stage12PersistenceStore:
    """DML-only store for the two API-owned temporary comparison tables."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
        open_session: SessionOpener = open_session_once,
    ) -> None:
        """Use ``session_factory`` if given, else build one from ``database_url``.

        Raises ValueError when neither is given or the URL is invalid.
        """
        self.sessions = StoreSessions.for_store(
            database_url,
            session_factory=session_factory,
            open_session=open_session,
            missing_url_message="Stage 12 database URL is required",
        )

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
            insert(ComparisonReport)
            .values(**values)
            .on_conflict_do_nothing(constraint=REPORT_IDENTITY_CONSTRAINT)
            .returning(*REPORT_ATTRIBUTES)
        )
        with self.sessions.transaction("create_or_resolve_report") as session:
            created = session.execute(statement).mappings().one_or_none()
            if created is not None:
                return ComparisonReportPersistenceResult(
                    record=_report(created),
                    created=True,
                )
            existing = (
                session.execute(
                    select(*REPORT_ATTRIBUTES).where(
                        *_identity_predicates(
                            ComparisonReport,
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
        with self.sessions.reading("get_report") as session:
            row = (
                session.execute(
                    select(*REPORT_ATTRIBUTES).where(
                        ComparisonReport.evaluation_id == evaluation_id
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
            select(*SIMULATION_ATTRIBUTES)
            .where(ComparisonSimulation.evaluation_id == evaluation_id)
            .order_by(
                ComparisonSimulation.role,
                ComparisonSimulation.simulation_execution_id,
            )
        )
        with self.sessions.reading("list_simulations") as session:
            rows = session.execute(statement).mappings().all()
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
        with self.sessions.transaction("replace_report") as session:
            row = (
                session.execute(
                    select(*REPORT_ATTRIBUTES)
                    .where(ComparisonReport.evaluation_id == record.evaluation_id)
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
                session.execute(
                    update(ComparisonReport)
                    .where(ComparisonReport.evaluation_id == record.evaluation_id)
                    .values(**{name: values[name] for name in REPORT_MUTABLE_COLUMNS})
                    .returning(*REPORT_ATTRIBUTES),
                    execution_options=WITHOUT_OBJECT_SYNC,
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
        with self.sessions.transaction("replace_report_result_comparison") as session:
            row = (
                session.execute(
                    select(*REPORT_ATTRIBUTES)
                    .where(ComparisonReport.evaluation_id == record.evaluation_id)
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
                session.execute(
                    update(ComparisonReport)
                    .where(ComparisonReport.evaluation_id == record.evaluation_id)
                    .values(
                        **{name: values[name] for name in REPORT_COMPARISON_COLUMNS}
                    )
                    .returning(*REPORT_ATTRIBUTES),
                    execution_options=WITHOUT_OBJECT_SYNC,
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
            insert(ComparisonSimulation)
            .values(**values)
            .on_conflict_do_nothing(constraint=SIMULATION_IDENTITY_CONSTRAINT)
            .returning(*SIMULATION_ATTRIBUTES)
        )
        with self.sessions.transaction("create_or_resolve_simulation") as session:
            parent_row = (
                session.execute(
                    select(*REPORT_ATTRIBUTES).where(
                        ComparisonReport.evaluation_id == record.evaluation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if parent_row is None:
                msg = f"comparison report {record.evaluation_id} does not exist"
                raise LookupError(msg)
            require_simulation_parent_matches(_report(parent_row), record)
            created = session.execute(statement).mappings().one_or_none()
            if created is not None:
                return ComparisonSimulationPersistenceResult(
                    record=_simulation(created),
                    created=True,
                )
            existing = (
                session.execute(
                    select(*SIMULATION_ATTRIBUTES).where(
                        *_identity_predicates(
                            ComparisonSimulation,
                            values,
                            SIMULATION_IDENTITY_COLUMNS,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                msg = (
                    "comparison simulation conflict did not resolve to an "
                    "existing identity"
                )
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
        with self.sessions.reading("get_simulation") as session:
            row = (
                session.execute(
                    select(*SIMULATION_ATTRIBUTES).where(
                        ComparisonSimulation.simulation_execution_id
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
        with self.sessions.transaction("replace_simulation") as session:
            row = (
                session.execute(
                    select(*SIMULATION_ATTRIBUTES)
                    .where(
                        ComparisonSimulation.simulation_execution_id
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
                session.execute(
                    update(ComparisonSimulation)
                    .where(
                        ComparisonSimulation.simulation_execution_id
                        == record.simulation_execution_id
                    )
                    .values(
                        **{name: values[name] for name in SIMULATION_MUTABLE_COLUMNS}
                    )
                    .returning(*SIMULATION_ATTRIBUTES),
                    execution_options=WITHOUT_OBJECT_SYNC,
                )
                .mappings()
                .one()
            )
            return _simulation(updated)

    def replace_simulation_if_status(
        self,
        record: ComparisonSimulationRecord,
        *,
        expected_status: ComparisonRunLifecycleStatus,
    ) -> tuple[ComparisonSimulationRecord, bool]:
        """Replace a simulation only while it remains in ``expected_status``."""
        values = _record_values(record)
        with self.sessions.transaction("replace_simulation_if_status") as session:
            row = (
                session.execute(
                    select(*SIMULATION_ATTRIBUTES)
                    .where(
                        ComparisonSimulation.simulation_execution_id
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
            if existing.status is not expected_status:
                return existing, False
            require_lifecycle_transition(existing.status, record.status)
            updated = (
                session.execute(
                    update(ComparisonSimulation)
                    .where(
                        ComparisonSimulation.simulation_execution_id
                        == record.simulation_execution_id
                    )
                    .values(
                        **{name: values[name] for name in SIMULATION_MUTABLE_COLUMNS}
                    )
                    .returning(*SIMULATION_ATTRIBUTES),
                    execution_options=WITHOUT_OBJECT_SYNC,
                )
                .mappings()
                .one()
            )
            return _simulation(updated), True

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
            update(ComparisonSimulation)
            .where(
                ComparisonSimulation.simulation_execution_id == simulation_execution_id,
                ComparisonSimulation.modal_invocation_id == expected_placeholder,
            )
            .values(
                modal_invocation_id=modal_invocation_id,
                updated_at=updated_at,
            )
            .returning(*SIMULATION_ATTRIBUTES)
        )
        with self.sessions.transaction("attach_simulation_invocation") as session:
            row = (
                session.execute(statement, execution_options=WITHOUT_OBJECT_SYNC)
                .mappings()
                .one_or_none()
            )
            if row is None:
                row = (
                    session.execute(
                        select(*SIMULATION_ATTRIBUTES).where(
                            ComparisonSimulation.simulation_execution_id
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
