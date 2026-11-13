"""A detailed-save call's writes to its report run and simulation runs.

A detailed-save call works on the Public API's run tables alone, as the
Modal worker role. ``ReportRunRecorder`` holds the coordinator's writes to
the report run its call names, and ``SimulationRunRecorder`` a worker's
writes to the simulation run its call claims, each under the call's own
Modal call ID. A run records a failure as one line, its error code and then
its summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from policyengine_stage12_persistence import (
    DistrictCodes,
    RunStatus,
    report_run_result,
)

from .failure_recording import persist_failure_record
from .failures import Stage12FailureDetail, failure_detail_from_record

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any
    from uuid import UUID

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.stage12_execution import (
        ReportRunIds,
        SimulationArtifactDescriptor,
    )
    from policyengine_stage12_persistence import RunWrite, SimulationRunState

    from .report_run_access import ReportRunWriter

# What a run records as its failure when its code is unknown.
UNKNOWN_FAILURE_CODE = "simulation_failed"


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
    return f"{error_code or UNKNOWN_FAILURE_CODE}: {error_summary or 'no summary'}"


def run_failure_detail(error_message: str | None) -> Stage12FailureDetail:
    """Return the failure a run recorded with ``run_error_message``.

    A message without a code, such as one the stuck-run sweep wrote, keeps
    the code its own first words give, or ``simulation_failed``.
    """
    error_code, separator, error_summary = (error_message or "").partition(": ")
    if not separator or not error_code or " " in error_code:
        return failure_detail_from_record(
            error_code=UNKNOWN_FAILURE_CODE, error_summary=error_message
        )
    return failure_detail_from_record(
        error_code=error_code, error_summary=error_summary
    )


@dataclass(frozen=True)
class ReportRunRecorder:
    """The coordinator's writes to the report run its call names, under its call ID."""

    writer: ReportRunWriter
    run_ids: ReportRunIds
    modal_call_id: str

    def claim(self) -> RunWrite:
        """Mark the report run running under this call, unless another call holds it."""
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


@dataclass(frozen=True)
class SimulationRunRecorder:
    """A worker's writes to the simulation run its call claims, under its call ID."""

    writer: ReportRunWriter
    simulation_run_id: UUID
    modal_call_id: str

    def claim(self) -> RunWrite:
        """Mark the run running under this call, unless another call holds it."""
        return self.writer.claim_simulation_run(
            self.simulation_run_id,
            modal_call_id=self.modal_call_id,
            claimed_at=datetime.now(UTC),
        )

    def state(self) -> SimulationRunState:
        """Return the run's state as it is now."""
        return self.writer.get_simulation_run(self.simulation_run_id)

    def still_held(self) -> bool:
        """Return whether this call still holds the run running."""
        state = self.state()
        return (
            state.status is RunStatus.RUNNING
            and state.modal_call_id == self.modal_call_id
        )

    def complete(self, descriptor: SimulationArtifactDescriptor) -> RunWrite:
        """Record the output file the descriptor names; mark the run succeeded."""
        return self.writer.complete_simulation_run(
            self.simulation_run_id,
            modal_call_id=self.modal_call_id,
            output_uri=descriptor.artifact.uri,
            output_sha256=descriptor.artifact.content_sha256,
            completed_at=datetime.now(UTC),
        )

    def fail(self, detail: Stage12FailureDetail) -> RunWrite:
        """Mark the run failed with ``detail`` while this call holds it."""
        return self.writer.fail_simulation_run(
            self.simulation_run_id,
            modal_call_id=self.modal_call_id,
            error_message=run_error_message(detail.error_code, detail.error_summary),
            failed_at=datetime.now(UTC),
        )
