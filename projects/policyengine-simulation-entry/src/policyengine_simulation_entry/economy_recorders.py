"""How sim records a finished v2 economy call: its stored files, or its result.

An economy simulation run's call returns the files it stored
(``EconomySimulationOutput``); sim links them to the run in the transaction
that marks it succeeded, and a later comparison reads the output file. A
report run's comparison call returns its ``SingleYearMacroOutput``, which the
persistence library's ``EconomyComparisonRecorder`` stores as rows in the
transaction that marks the comparison step succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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
    from collections.abc import Callable
    from datetime import datetime

    from policyengine_simulation_persistence import SimulationRunRecord
    from sqlalchemy import Connection

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
