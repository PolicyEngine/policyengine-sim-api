"""How sim records a finished v2 economy call: its stored files, or its result.

An economy simulation run's call returns the files it stored
(``EconomySimulationOutput``); sim links them to the run in the transaction
that marks it succeeded, and a later comparison reads the output file. A
report run's comparison call returns its ``SingleYearMacroOutput``, which the
persistence library's ``EconomyComparisonRecorder`` stores as rows in the
transaction that marks the comparison step succeeded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from policyengine_simulation_contract.call_reports import CallReportStatus
from policyengine_simulation_contract.economy_runs import EconomySimulationOutput
from policyengine_simulation_persistence import (
    EconomyComparisonRecorder,
    OutputFile,
    ReportKind,
    SimulationKind,
    record_output_files,
)
from policyengine_simulation_persistence.run_store import utc_now

from policyengine_simulation_entry.run_recording import ResultRecorders

if TYPE_CHECKING:
    from collections.abc import Callable, Collection
    from datetime import datetime
    from uuid import UUID

    from policyengine_simulation_contract.call_reports import CallEndedReport
    from policyengine_simulation_persistence import SimulationRunRecord
    from sqlalchemy import Connection

logger = logging.getLogger(__name__)

# The roles of the files an economy simulation run stores.
SIMULATION_INPUT_ROLE = "simulation_input"
SIMULATION_OUTPUT_ROLE = "simulation_output"


@dataclass(frozen=True)
class EconomySimulationRecorder:
    """Validate an economy simulation call's output and link its stored files."""

    clock: Callable[[], datetime] = utc_now

    def validate(self, output: object) -> EconomySimulationOutput:
        """Return the call's output; raise ValueError when it is not one."""
        return EconomySimulationOutput.model_validate(output)

    def write(
        self,
        connection: Connection,
        runs: tuple[SimulationRunRecord, ...],
        result: object,
    ) -> None:
        """Link the run's input and output files in the transaction succeeding it."""
        if not isinstance(result, EconomySimulationOutput):
            msg = "the economy recorder writes only validated simulation outputs"
            raise TypeError(msg)
        written_at = self.clock()
        for run in runs:
            record_output_files(
                connection,
                [
                    OutputFile(
                        SIMULATION_INPUT_ROLE,
                        result.input.uri,
                        result.input.content_sha256,
                    ),
                    OutputFile(
                        SIMULATION_OUTPUT_ROLE,
                        result.output.uri,
                        result.output.content_sha256,
                    ),
                ],
                written_at=written_at,
                simulation_run_id=run.id,
            )


def economy_result_recorders() -> ResultRecorders:
    """Return the recorders of economy simulation runs and comparison steps."""
    return ResultRecorders(
        simulations={SimulationKind.ECONOMY: EconomySimulationRecorder()},
        comparisons={ReportKind.ECONOMY: EconomyComparisonRecorder()},
    )


class WaitingComparisons(Protocol):
    """Start comparison steps whose runs have succeeded (``EconomyReports``)."""

    async def start_comparisons_after(
        self, simulation_run_ids: Collection[UUID]
    ) -> None:
        """Start the comparisons waiting for these runs that can start now."""
        ...


class ReportedCallRecorder(Protocol):
    """Record a reported call's outcome (``RunRecorder``)."""

    async def record_reported_call(self, report: CallEndedReport) -> CallReportStatus:
        """Record the call's outcome if Modal has one."""
        ...


# The answers that mean the reported call's outcome is recorded.
RECORDED_STATUSES = frozenset(
    {CallReportStatus.RECORDED, CallReportStatus.ALREADY_RECORDED}
)


@dataclass(frozen=True)
class ComparisonStartingRecorder:
    """Record a reported call, then start the comparisons its runs complete.

    A simulation call that reports its end usually completes a report run's
    pair of runs, so its comparison starts at once. A failure to start one
    is logged and changes nothing in the answer: the safety check starts
    comparisons that are ready.
    """

    recorder: ReportedCallRecorder
    comparisons: WaitingComparisons

    async def record_reported_call(self, report: CallEndedReport) -> CallReportStatus:
        """Record the reported call; start the comparisons it lets start."""
        report_status = await self.recorder.record_reported_call(report)
        if report.simulation_run_ids and report_status in RECORDED_STATUSES:
            try:
                await self.comparisons.start_comparisons_after(
                    report.simulation_run_ids
                )
            except Exception as error:  # noqa: BLE001  # logged; the safety check starts it later
                logger.warning(
                    "report_run_comparisons_not_started",
                    extra={
                        "modal_call_id": report.modal_call_id,
                        "error_type": type(error).__name__,
                    },
                )
        return report_status
