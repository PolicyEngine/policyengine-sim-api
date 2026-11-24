"""Conditional writes of a simulation run's cliff calculation, as the Modal worker role.

API v1's ``cliff_impacts`` holds each cliff calculation of a
simulation run, its cliff result. The simulation service inserts it pending
and may record the Modal call it starts while it is still pending with none.
A simulation worker's call then claims it under that call, as a run is
claimed, and records the cliff gap and share, or the failure, only while its
call holds it running. Each write is one ``UPDATE`` conditional on the row's
status and recorded call, so a call Modal restarts, or a second start of the
same calculation, changes nothing already final.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from policyengine_simulation_persistence.tables import CliffImpact
from sqlalchemy import func, or_, select, update

from policyengine_stage12_persistence.report_run_store import (
    ACTIVE_STATUSES,
    ReportRunStore,
    RunStatus,
    running_under_call,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from policyengine_stage12_persistence.report_run_store import RunWrite


@dataclass(frozen=True)
class CliffResultState:
    """A cliff result's run, status, recorded call, values and failure.

    ``modal_call_id`` is None while no call is recorded; ``cliff_gap`` and
    ``cliff_share`` are set only once it succeeded, and ``error_message``
    only once it failed.
    """

    simulation_run_id: UUID
    status: RunStatus
    modal_call_id: str | None
    cliff_gap: float | None
    cliff_share: float | None
    error_message: str | None


class CliffResultStore(ReportRunStore):
    """A run store that also claims and records simulation runs' cliff results.

    The worker reads the cliff result's simulation run with the run store's
    ``get_simulation_run``.
    """

    def get_cliff_result(self, cliff_result_id: UUID) -> CliffResultState:
        """Return a cliff result's state; it only reads the row.

        Raises LookupError when the cliff result does not exist.
        """
        statement = select(
            CliffImpact.simulation_run_id,
            CliffImpact.status,
            CliffImpact.modal_call_id,
            CliffImpact.cliff_gap,
            CliffImpact.cliff_share,
            CliffImpact.error_message,
        ).where(CliffImpact.id == cliff_result_id)
        with self.sessions.transaction("get_cliff_result") as session:
            stored = session.execute(statement).one_or_none()
        if stored is None:
            msg = f"cliff result {cliff_result_id} does not exist"
            raise LookupError(msg)
        return CliffResultState(
            simulation_run_id=stored.simulation_run_id,
            status=RunStatus(stored.status),
            modal_call_id=stored.modal_call_id,
            cliff_gap=stored.cliff_gap,
            cliff_share=stored.cliff_share,
            error_message=stored.error_message,
        )

    def claim_cliff_result(
        self,
        cliff_result_id: UUID,
        *,
        modal_call_id: str,
        claimed_at: datetime,
    ) -> RunWrite:
        """Mark the cliff result running under the worker's ``modal_call_id``.

        A pending or running cliff result is claimed while it records no call
        or this call: the simulation service records the call it starts while
        the row is still pending, and Modal restarts a pre-empted call under
        the same call ID. One recording another call is that call's.
        """
        statement = (
            update(CliffImpact)
            .where(
                CliffImpact.id == cliff_result_id,
                CliffImpact.status.in_(ACTIVE_STATUSES),
                or_(
                    CliffImpact.modal_call_id.is_(None),
                    CliffImpact.modal_call_id == modal_call_id,
                ),
            )
            .values(
                status=RunStatus.RUNNING.value,
                modal_call_id=modal_call_id,
                started_at=func.coalesce(CliffImpact.started_at, claimed_at),
                updated_at=claimed_at,
            )
        )
        return self._write(
            CliffImpact, cliff_result_id, statement, "claim_cliff_result"
        )

    def complete_cliff_result(
        self,
        cliff_result_id: UUID,
        *,
        modal_call_id: str,
        cliff_gap: float,
        cliff_share: float,
        completed_at: datetime,
    ) -> RunWrite:
        """Record the cliff gap and share; mark it succeeded while this call holds it.

        A cliff result failed meanwhile, as the stuck-run sweep fails one,
        takes no values from the call.
        """
        statement = (
            update(CliffImpact)
            .where(
                CliffImpact.id == cliff_result_id,
                running_under_call(CliffImpact, modal_call_id),
            )
            .values(
                status=RunStatus.SUCCEEDED.value,
                cliff_gap=cliff_gap,
                cliff_share=cliff_share,
                error_message=None,
                completed_at=completed_at,
                updated_at=completed_at,
            )
        )
        return self._write(
            CliffImpact, cliff_result_id, statement, "complete_cliff_result"
        )

    def fail_cliff_result(
        self,
        cliff_result_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: datetime,
    ) -> RunWrite:
        """Mark the cliff result failed while ``modal_call_id`` holds it running."""
        statement = (
            update(CliffImpact)
            .where(
                CliffImpact.id == cliff_result_id,
                running_under_call(CliffImpact, modal_call_id),
            )
            .values(
                status=RunStatus.FAILED.value,
                error_message=error_message,
                completed_at=failed_at,
                updated_at=failed_at,
            )
        )
        return self._write(CliffImpact, cliff_result_id, statement, "fail_cliff_result")
