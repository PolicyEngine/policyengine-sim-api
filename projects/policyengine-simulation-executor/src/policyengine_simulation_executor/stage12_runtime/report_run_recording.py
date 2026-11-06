"""Recording a report call's report run and simulation runs beside its Stage 12 records.

A call that names a report run keeps the comparison path's Stage 12 records,
which coordinate its workers. ``SimulationRunRecordingStore`` mirrors every
conditional claim, success and failure of a child record onto that child's
simulation run, as the Modal worker role; ``ReportRunRecorder`` holds the
coordinator's writes to the report run. A mirrored write can be lost, so
before it completes or fails a report run the coordinator also gives both
simulation runs their child records' outcomes, and a failed report fails the
child records still running.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
)
from policyengine_stage12_persistence import (
    DistrictCodes,
    RunStatus,
    report_run_result,
)

from .failure_recording import persist_failure_record

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any
    from uuid import UUID

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.stage12_execution import (
        ComparisonReportPersistenceResult,
        ComparisonReportRecord,
        ComparisonSimulationPersistenceResult,
        ComparisonSimulationRecord,
        ReportRunIds,
    )
    from policyengine_stage12_persistence import RunWrite

    from .dependencies import ComparisonStore
    from .failures import Stage12FailureDetail
    from .report_run_access import ReportRunWriter


logger = logging.getLogger(__name__)


def us_district_codes() -> DistrictCodes:
    """Return .py's US state FIPS codes and at-large states, for district rows."""
    # .py's US data loads only for a US report's result.
    from policyengine.countries.us.data import (
        AT_LARGE_STATES,
        US_STATE_FIPS,
    )

    return DistrictCodes(
        state_fips={state: int(fips) for state, fips in US_STATE_FIPS.items()},
        at_large_states=frozenset(AT_LARGE_STATES),
    )


def run_error_message(error_code: str | None, error_summary: str | None) -> str:
    """Return the redacted failure a run records: its code, then its summary."""
    return f"{error_code or 'simulation_failed'}: {error_summary or 'no summary'}"


@dataclass(frozen=True)
class ReportRunRecorder:
    """The coordinator's writes to the report run its call names, under its call ID."""

    writer: ReportRunWriter
    run_ids: ReportRunIds
    modal_call_id: str

    def claim(self) -> RunWrite:
        """Check the run names the call's simulation runs, then claim it."""
        self.writer.require_simulation_runs(self.run_ids)
        return self.writer.claim_report_run(
            self.run_ids.report_run_id,
            modal_call_id=self.modal_call_id,
            claimed_at=datetime.now(UTC),
        )

    def complete(self, economy_output: Mapping[str, Any], *, country: str) -> RunWrite:
        """Mark the report run succeeded, storing its economy output.

        The output's columns of the report run are set in the same update,
        and its result rows written in the same transaction, only while this
        call holds the run. An output that cannot be stored raises ValueError.
        """
        return self.writer.complete_report_run(
            self.run_ids,
            modal_call_id=self.modal_call_id,
            completed_at=datetime.now(UTC),
            result=report_run_result(
                economy_output,
                run_ids=self.run_ids,
                country=country,
                district_codes=us_district_codes() if country == "us" else None,
            ),
        )

    def fail_quietly(
        self, detail: Stage12FailureDetail, *, runtime: ObservabilityRuntime | None
    ) -> None:
        """Fail the report run, logging rather than raising a failed write."""
        persist_failure_record(
            partial(self.fail, detail), runtime=runtime, failure_scope="report_run"
        )

    def fail(self, detail: Stage12FailureDetail) -> RunWrite:
        """Mark the report run failed with ``detail`` while this call holds it."""
        return self.writer.fail_report_run(
            self.run_ids.report_run_id,
            modal_call_id=self.modal_call_id,
            error_message=run_error_message(detail.error_code, detail.error_summary),
            failed_at=datetime.now(UTC),
        )


class SimulationRunRecordingStore:
    """A ``ComparisonStore`` that also records each child's simulation run.

    ``modal_call_id`` is the worker's own call, recorded on its simulation run
    when it claims it; the coordinator never claims one and passes None.
    """

    def __init__(
        self,
        store: ComparisonStore,
        runs: ReportRunWriter,
        run_ids: ReportRunIds,
        *,
        modal_call_id: str | None = None,
    ) -> None:
        """Record on ``run_ids``'s simulation runs through ``runs``."""
        self._store = store
        self._runs = runs
        self._run_ids = run_ids
        self._modal_call_id = modal_call_id

    def create_or_resolve_report(
        self, record: ComparisonReportRecord
    ) -> ComparisonReportPersistenceResult:
        """Delegate to the Stage 12 store."""
        return self._store.create_or_resolve_report(record)

    def get_report(self, evaluation_id: UUID) -> ComparisonReportRecord:
        """Delegate to the Stage 12 store."""
        return self._store.get_report(evaluation_id)

    def replace_report(self, record: ComparisonReportRecord) -> ComparisonReportRecord:
        """Delegate to the Stage 12 store."""
        return self._store.replace_report(record)

    def replace_report_result_comparison(
        self, record: ComparisonReportRecord
    ) -> ComparisonReportRecord:
        """Delegate to the Stage 12 store."""
        return self._store.replace_report_result_comparison(record)

    def create_or_resolve_simulation(
        self, record: ComparisonSimulationRecord
    ) -> ComparisonSimulationPersistenceResult:
        """Delegate to the Stage 12 store."""
        return self._store.create_or_resolve_simulation(record)

    def get_simulation(
        self, simulation_execution_id: UUID
    ) -> ComparisonSimulationRecord:
        """Delegate to the Stage 12 store."""
        return self._store.get_simulation(simulation_execution_id)

    def replace_simulation(
        self, record: ComparisonSimulationRecord
    ) -> ComparisonSimulationRecord:
        """Delegate to the Stage 12 store; a dispatch placeholder records nothing."""
        return self._store.replace_simulation(record)

    def attach_simulation_invocation(
        self,
        simulation_execution_id: UUID,
        *,
        expected_placeholder: str,
        modal_invocation_id: str,
        updated_at: datetime,
    ) -> ComparisonSimulationRecord:
        """Delegate to the Stage 12 store."""
        return self._store.attach_simulation_invocation(
            simulation_execution_id,
            expected_placeholder=expected_placeholder,
            modal_invocation_id=modal_invocation_id,
            updated_at=updated_at,
        )

    def replace_simulation_if_status(
        self,
        record: ComparisonSimulationRecord,
        *,
        expected_status: ComparisonRunLifecycleStatus,
    ) -> tuple[ComparisonSimulationRecord, bool]:
        """Write the child record, then its simulation run once the record changed.

        A worker Modal restarts after its record succeeded, but before its
        simulation run did, completes the run from that record (see
        ``complete_from_succeeded_child``), so both name the same output
        file. A simulation run that is already failed refuses the child's
        claim, which its worker treats as a cancellation.
        """
        simulation_run_id = self._run_ids.simulation_run_id(record.role)
        written_at = record.updated_at or datetime.now(UTC)
        stored, replaced = self._store.replace_simulation_if_status(
            record, expected_status=expected_status
        )
        if not replaced:
            return stored, replaced
        if record.status is ComparisonRunLifecycleStatus.SUCCEEDED:
            # The child's result is recorded; if this second write is lost,
            # the coordinator completes the run from the child's record.
            try:
                self._complete(simulation_run_id, record, written_at)
            except Exception as error:  # noqa: BLE001  # a lost second write is completed again by the coordinator
                logger.warning(
                    "Could not complete simulation run %s (%s)",
                    simulation_run_id,
                    type(error).__name__,
                )
        elif record.status is ComparisonRunLifecycleStatus.RUNNING:
            claimed = self._runs.claim_simulation_run(
                simulation_run_id,
                modal_call_id=self._require_modal_call_id(),
                claimed_at=written_at,
            )
            if claimed.status is RunStatus.FAILED:
                return stored, False
        elif record.status is ComparisonRunLifecycleStatus.FAILED:
            self._runs.fail_simulation_run(
                simulation_run_id,
                error_message=run_error_message(
                    record.error_code, record.error_summary
                ),
                failed_at=record.completed_at or written_at,
            )
        return stored, replaced

    def reconcile_simulation_run(self, simulation_execution_id: UUID) -> None:
        """Give a child's simulation run its finished Stage 12 record's outcome.

        A worker or coordinator that lost its second write leaves the run
        behind its record; each write here is conditional, so a run that
        already has its outcome is left as it is. An unfinished child, or one
        never created, changes nothing.
        """
        try:
            record = self._store.get_simulation(simulation_execution_id)
        except LookupError:
            return
        if record.status is ComparisonRunLifecycleStatus.SUCCEEDED:
            self.complete_from_succeeded_child(record)
        elif record.status is ComparisonRunLifecycleStatus.FAILED:
            self._runs.fail_simulation_run(
                self._run_ids.simulation_run_id(record.role),
                error_message=run_error_message(
                    record.error_code, record.error_summary
                ),
                failed_at=record.completed_at or datetime.now(UTC),
            )

    def complete_from_succeeded_child(self, record: ComparisonSimulationRecord) -> None:
        """Complete a child's simulation run from its succeeded Stage 12 record."""
        self._complete(
            self._run_ids.simulation_run_id(record.role),
            record,
            record.updated_at or datetime.now(UTC),
        )

    def _complete(
        self,
        simulation_run_id: UUID,
        record: ComparisonSimulationRecord,
        written_at: datetime,
    ) -> RunStatus:
        if record.output_uri is None or record.output_sha256 is None:
            msg = "a succeeded simulation must name its output file"
            raise ValueError(msg)
        return self._runs.complete_simulation_run(
            simulation_run_id,
            output_uri=record.output_uri,
            output_sha256=record.output_sha256,
            completed_at=record.completed_at or written_at,
        ).status

    def _require_modal_call_id(self) -> str:
        if self._modal_call_id is None:
            msg = "only a worker with its Modal call ID claims a simulation run"
            raise ValueError(msg)
        return self._modal_call_id
