"""The Modal worker role's database credentials, which runs and household jobs use.

An operator creates the Modal secret in each Modal environment. The v2 Modal
application attaches it to the coordinator and the simulation workers, which
record the status and results of the report run and simulation runs their
call names, and to the household functions, which record the household job
their call names. The secret must exist in a Modal environment before the v2
application deploys there: ``modal deploy`` checks its required key and fails
without it.
"""

from __future__ import annotations

import os
from functools import partial
from typing import TYPE_CHECKING, Protocol

from policyengine_stage12_persistence import ReportRunStore

from .database_retry import open_connection_with_retry

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from typing import Any
    from uuid import UUID

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.stage12_execution import ReportRunIds
    from policyengine_stage12_persistence import (
        ReportRunResult,
        RunWrite,
        SimulationRunState,
    )

# The Modal secret and the one key it must hold: a postgresql:// URL for the
# Modal worker role on the database that holds the Public API's run tables.
MODAL_WORKER_DATABASE_SECRET_NAME = "policyengine-modal-worker-database"  # noqa: S105  # the Modal secret's name, not its value
MODAL_WORKER_DATABASE_URL_ENV = "MODAL_WORKER_DATABASE_URL"


class ReportRunWriter(Protocol):
    """Report run and simulation run writes; ``ReportRunStore`` implements it.

    Each write is conditional on the run's status and returns whether it
    changed the run, and the run's status afterwards. The two reads,
    ``require_simulation_runs`` and ``get_simulation_run``, change nothing.
    """

    def require_simulation_runs(self, run_ids: ReportRunIds) -> None:
        """Raise unless the report run names the call's two simulation runs.

        LookupError when the report run does not exist; ValueError when it
        names other simulation runs.
        """
        ...

    def get_simulation_run(self, simulation_run_id: UUID) -> SimulationRunState:
        """Return a simulation run's status, year, versions, dataset, region and output.

        LookupError when the run does not exist.
        """
        ...

    def claim_report_run(
        self, report_run_id: UUID, *, modal_call_id: str, claimed_at: datetime
    ) -> RunWrite:
        """Mark a pending or running report run that records no other call running."""
        ...

    def complete_report_run(
        self,
        run_ids: ReportRunIds,
        *,
        modal_call_id: str,
        completed_at: datetime,
        result: ReportRunResult | None = None,
    ) -> RunWrite:
        """Mark the report run succeeded with ``result`` while this call holds it."""
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


class HouseholdJobWriter(Protocol):
    """Household job writes; ``ReportRunStore`` implements it.

    Each write is conditional on the job's status and recorded Modal call,
    and returns whether it changed the job, and the job's status afterwards.
    """

    def claim_household_job(
        self, household_job_id: UUID, *, modal_call_id: str, claimed_at: datetime
    ) -> RunWrite:
        """Mark a pending or running job that records no other call running."""
        ...

    def complete_household_job(
        self,
        household_job_id: UUID,
        *,
        modal_call_id: str,
        result: Mapping[str, Any],
        completed_at: datetime,
    ) -> RunWrite:
        """Mark the job succeeded with ``result`` while this call holds it."""
        ...

    def fail_household_job(
        self,
        household_job_id: UUID,
        *,
        modal_call_id: str,
        error_message: str,
        failed_at: datetime,
        result: Mapping[str, Any] | None = None,
    ) -> RunWrite:
        """Mark the job failed while ``modal_call_id`` holds it."""
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
