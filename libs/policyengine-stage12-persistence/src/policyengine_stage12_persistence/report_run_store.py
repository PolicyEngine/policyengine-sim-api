"""Conditional writes of report run and simulation run status, as the Modal worker role.

Every write is one ``UPDATE`` conditional on the run's expected status, so a
call that Modal restarts after pre-emption, or a second writer, changes
nothing that is already final.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select, update

from policyengine_stage12_persistence.report_run_tables import (
    report_run_simulation_runs,
    report_runs,
    simulation_runs,
)
from policyengine_stage12_persistence.store import (
    ConnectionOpener,
    create_stage12_engine,
    open_connection_once,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_contract.stage12_execution import ReportRunIds
    from sqlalchemy import Engine, Table, Update
    from sqlalchemy.engine import Connection


class RunStatus(StrEnum):
    """A report run's or simulation run's status."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


ACTIVE_STATUSES = (RunStatus.PENDING.value, RunStatus.RUNNING.value)


@dataclass(frozen=True)
class RunWrite:
    """One conditional write's outcome: whether it changed the run, and its status."""

    changed: bool
    status: RunStatus


class ReportRunStore:
    """Status writes to the Public API's report runs and simulation runs."""

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
                msg = "report run database URL is required"
                raise ValueError(msg)
            engine = create_stage12_engine(database_url)
        self.engine = engine
        self._open_connection = open_connection

    @contextmanager
    def _transaction(self, operation_name: str) -> Iterator[Connection]:
        # As in the Stage 12 store, the opener only opens the connection; every
        # statement and the COMMIT run after it returns.
        connection = self._open_connection(
            self.engine.connect,
            operation_name=operation_name,
        )
        with connection, connection.begin():
            yield connection

    def require_simulation_runs(self, run_ids: ReportRunIds) -> None:
        """Raise ValueError unless the report run combines both simulation runs."""
        statement = select(
            report_run_simulation_runs.c.role,
            report_run_simulation_runs.c.simulation_run_id,
        ).where(report_run_simulation_runs.c.report_run_id == run_ids.report_run_id)
        with self._transaction("require_simulation_runs") as connection:
            members = {
                (row.role, row.simulation_run_id)
                for row in connection.execute(statement)
            }
        expected = {
            ("baseline", run_ids.baseline_simulation_run_id),
            ("reform", run_ids.reform_simulation_run_id),
        }
        if not expected <= members:
            msg = "the report run does not combine the simulation runs its call names"
            raise ValueError(msg)

    def claim_report_run(
        self,
        report_run_id: UUID,
        *,
        modal_call_id: str,
        claimed_at: datetime,
    ) -> RunWrite:
        """Mark the report run running under ``modal_call_id``.

        A pending run is claimed, and so is a running one that no other call
        holds: the simulation service may mark it running before the call
        starts, and Modal restarts a pre-empted call under the same call ID.
        """
        statement = (
            update(report_runs)
            .where(
                report_runs.c.id == report_run_id,
                or_(
                    report_runs.c.status == RunStatus.PENDING.value,
                    and_(
                        report_runs.c.status == RunStatus.RUNNING.value,
                        or_(
                            report_runs.c.modal_call_id.is_(None),
                            report_runs.c.modal_call_id == modal_call_id,
                        ),
                    ),
                ),
            )
            .values(
                status=RunStatus.RUNNING.value,
                modal_call_id=modal_call_id,
                started_at=func.coalesce(report_runs.c.started_at, claimed_at),
                updated_at=claimed_at,
            )
        )
        return self._write(report_runs, report_run_id, statement, "claim_report_run")

    def complete_report_run(
        self,
        run_ids: ReportRunIds,
        *,
        modal_call_id: str,
        completed_at: datetime,
    ) -> RunWrite:
        """Mark the report run succeeded while ``modal_call_id`` holds it running."""
        statement = (
            update(report_runs)
            .where(
                report_runs.c.id == run_ids.report_run_id,
                report_runs.c.status == RunStatus.RUNNING.value,
                report_runs.c.modal_call_id == modal_call_id,
            )
            .values(
                status=RunStatus.SUCCEEDED.value,
                error_message=None,
                completed_at=completed_at,
                updated_at=completed_at,
            )
        )
        return self._write(
            report_runs, run_ids.report_run_id, statement, "complete_report_run"
        )

    def fail_report_run(
        self,
        report_run_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: datetime,
    ) -> RunWrite:
        """Mark the report run failed while ``modal_call_id`` holds it running."""
        statement = (
            update(report_runs)
            .where(
                report_runs.c.id == report_run_id,
                report_runs.c.status == RunStatus.RUNNING.value,
                report_runs.c.modal_call_id == modal_call_id,
            )
            .values(
                status=RunStatus.FAILED.value,
                error_message=error_message,
                completed_at=failed_at,
                updated_at=failed_at,
            )
        )
        return self._write(report_runs, report_run_id, statement, "fail_report_run")

    def claim_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        modal_call_id: str,
        claimed_at: datetime,
    ) -> RunWrite:
        """Mark a pending or running simulation run running under ``modal_call_id``.

        A running one moves to the newest call, because the coordinator starts
        a simulation again only after stopping its earlier call.
        """
        statement = (
            update(simulation_runs)
            .where(
                simulation_runs.c.id == simulation_run_id,
                simulation_runs.c.status.in_(ACTIVE_STATUSES),
            )
            .values(
                status=RunStatus.RUNNING.value,
                modal_call_id=modal_call_id,
                started_at=func.coalesce(simulation_runs.c.started_at, claimed_at),
                updated_at=claimed_at,
            )
        )
        return self._write(
            simulation_runs, simulation_run_id, statement, "claim_simulation_run"
        )

    def complete_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        output_uri: str,
        output_sha256: str,
        completed_at: datetime,
    ) -> RunWrite:
        """Record a running simulation run's output file and mark it succeeded."""
        statement = (
            update(simulation_runs)
            .where(
                simulation_runs.c.id == simulation_run_id,
                simulation_runs.c.status == RunStatus.RUNNING.value,
            )
            .values(
                status=RunStatus.SUCCEEDED.value,
                output_uri=output_uri,
                output_sha256=output_sha256,
                error_message=None,
                completed_at=completed_at,
                updated_at=completed_at,
            )
        )
        return self._write(
            simulation_runs, simulation_run_id, statement, "complete_simulation_run"
        )

    def fail_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        error_message: str,
        failed_at: datetime,
    ) -> RunWrite:
        """Mark a pending or running simulation run failed with ``error_message``."""
        statement = (
            update(simulation_runs)
            .where(
                simulation_runs.c.id == simulation_run_id,
                simulation_runs.c.status.in_(ACTIVE_STATUSES),
            )
            .values(
                status=RunStatus.FAILED.value,
                error_message=error_message,
                completed_at=failed_at,
                updated_at=failed_at,
            )
        )
        return self._write(
            simulation_runs, simulation_run_id, statement, "fail_simulation_run"
        )

    def _write(
        self,
        table: Table,
        run_id: UUID,
        statement: Update,
        operation_name: str,
    ) -> RunWrite:
        """Run a conditional update; return whether it changed the run, and its status.

        Raises LookupError when the run does not exist.
        """
        with self._transaction(operation_name) as connection:
            changed = connection.execute(
                statement.returning(table.c.status)
            ).scalar_one_or_none()
            if changed is not None:
                return RunWrite(changed=True, status=RunStatus(changed))
            current = connection.execute(
                select(table.c.status).where(table.c.id == run_id)
            ).scalar_one_or_none()
        if current is None:
            noun = "report run" if table is report_runs else "simulation run"
            msg = f"{noun} {run_id} does not exist"
            raise LookupError(msg)
        return RunWrite(changed=False, status=RunStatus(current))
