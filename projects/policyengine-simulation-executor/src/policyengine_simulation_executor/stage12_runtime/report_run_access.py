"""The Modal worker role's database credentials, which report runs are recorded with.

An operator creates the Modal secret in each Modal environment. The v2 Modal
application attaches it to the coordinator and the simulation workers, which
record the status and results of the report run and simulation runs their
call names.
"""

from __future__ import annotations

import os
from functools import partial
from typing import TYPE_CHECKING, Protocol

from policyengine_stage12_persistence import ReportRunStore

from .database_retry import open_connection_with_retry

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.stage12_execution import ReportRunIds
    from policyengine_stage12_persistence import RunWrite

# The Modal secret and the one key it must hold: a postgresql:// URL for the
# Modal worker role on the database that holds the Public API's run tables.
MODAL_WORKER_DATABASE_SECRET_NAME = "policyengine-modal-worker-database"  # noqa: S105  # the Modal secret's name, not its value
MODAL_WORKER_DATABASE_URL_ENV = "MODAL_WORKER_DATABASE_URL"


class ReportRunWriter(Protocol):
    """Report run and simulation run writes; ``ReportRunStore`` implements it.

    Each write is conditional on the run's status and returns whether it
    changed the run, and the run's status afterwards.
    """

    def require_simulation_runs(self, run_ids: ReportRunIds) -> None:
        """Raise ValueError unless the report run combines both simulation runs."""
        ...

    def claim_report_run(
        self, report_run_id: UUID, *, modal_call_id: str, claimed_at: datetime
    ) -> RunWrite:
        """Mark a pending report run, or one no other call holds, running."""
        ...

    def complete_report_run(
        self,
        run_ids: ReportRunIds,
        *,
        modal_call_id: str,
        completed_at: datetime,
    ) -> RunWrite:
        """Mark the report run succeeded while ``modal_call_id`` holds it."""
        ...

    def fail_report_run(
        self,
        report_run_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: datetime,
    ) -> RunWrite:
        """Mark the report run failed while ``modal_call_id`` holds it."""
        ...

    def claim_simulation_run(
        self, simulation_run_id: UUID, *, modal_call_id: str, claimed_at: datetime
    ) -> RunWrite:
        """Mark a pending or running simulation run running under this call."""
        ...

    def complete_simulation_run(
        self,
        simulation_run_id: UUID,
        *,
        output_uri: str,
        output_sha256: str,
        completed_at: datetime,
    ) -> RunWrite:
        """Record a running simulation run's output file and mark it succeeded."""
        ...

    def fail_simulation_run(
        self, simulation_run_id: UUID, *, error_message: str, failed_at: datetime
    ) -> RunWrite:
        """Mark a pending or running simulation run failed."""
        ...


def report_run_store(runtime: ObservabilityRuntime | None = None) -> ReportRunStore:
    """Build the report run store from ``MODAL_WORKER_DATABASE_URL``.

    Raises ``ValueError`` when the variable is unset or not a complete
    ``postgresql://`` URL.
    """
    return ReportRunStore(
        os.environ.get(MODAL_WORKER_DATABASE_URL_ENV, ""),
        open_connection=partial(open_connection_with_retry, runtime=runtime),
    )
