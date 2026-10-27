"""Record a household function's outcome on the runs its call computes.

One call computes one household calculation (one simulation run) or a
household report's baseline and reform (two runs sharing the call). The
request names each policy by its run (``policy_key_for_run``), so each
result maps back to its run.

A ``HouseholdExecutionResult`` stores each run's calculated household, with
its warnings, SPM receipt, the applied SPM selection and the bundle, in the
transaction that marks the runs succeeded. A ``HouseholdExecutionFailure``
(inputs the country model does not know, an unusable SPM selection, another
bundle, or a failure inside the calculation) fails the runs with
``household_calculation_failed``: its message names only the failure's code,
and the household error document is the run's error detail, which the
calculation's reader returns.

This is v2 code: the main path's ``run_simulation`` does not use it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionFailure,
    HouseholdExecutionResult,
    parse_household_execution_outcome,
)
from policyengine_simulation_persistence import (
    HouseholdOutput,
    RunError,
    RunErrorCode,
    write_household_output,
)

from policyengine_simulation_entry.run_recording import CalculationFailedError

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from policyengine_simulation_contract.household_execution import (
        HouseholdPolicyResult,
    )
    from policyengine_simulation_persistence import SimulationRunRecord
    from sqlalchemy import Connection

    type OutputWriter = Callable[
        [Connection, SimulationRunRecord, HouseholdOutput], None
    ]


def policy_key_for_run(run_id: UUID) -> str:
    """Return the policy key that names a run's policy in a household request."""
    return f"run-{run_id.hex}"


def _household_output(
    result: HouseholdExecutionResult, policy_result: HouseholdPolicyResult
) -> HouseholdOutput:
    output = result.output
    return HouseholdOutput(
        schema_version=result.schema_version,
        household=policy_result.household.model_dump(mode="json"),
        warnings=policy_result.warnings,
        spm_config=(
            None
            if output.spm_config is None
            else output.spm_config.model_dump(mode="json")
        ),
        spm_provenance=(
            None
            if policy_result.spm_provenance is None
            else policy_result.spm_provenance.model_dump(mode="json")
        ),
        bundle=output.bundle.model_dump(mode="json"),
    )


class HouseholdResultRecorder:
    """Validate a household function's outcome and write each run's household."""

    def __init__(self, *, write_output: OutputWriter = write_household_output) -> None:
        """Write each run's household with ``write_output``."""
        self._write_output = write_output

    def validate(self, output: object) -> HouseholdExecutionResult:
        """Return the successful outcome.

        Raise CalculationFailedError for a typed failure, and ValueError for
        an output that is neither.
        """
        outcome = parse_household_execution_outcome(output)
        if isinstance(outcome, HouseholdExecutionFailure):
            raise CalculationFailedError(
                RunError(
                    RunErrorCode.HOUSEHOLD_CALCULATION_FAILED,
                    f"The household calculation failed: {outcome.error.code}.",
                    detail=outcome.error.model_dump(mode="json"),
                )
            )
        return outcome

    def write(
        self,
        connection: Connection,
        runs: tuple[SimulationRunRecord, ...],
        result: object,
    ) -> None:
        """Write each run's household in the transaction that succeeds them.

        Raise CalculationFailedError, writing nothing, when the outcome has no
        result for one of the runs: the output cannot describe those runs, so
        they fail as an invalid result instead of waiting for it to change.
        """
        if not isinstance(result, HouseholdExecutionResult):
            msg = "a household result recorder writes household results only"
            raise TypeError(msg)
        by_policy_key = {
            policy_result.policy_key: policy_result
            for policy_result in result.output.results
        }
        outputs = []
        for run in runs:
            policy_result = by_policy_key.get(policy_key_for_run(run.id))
            if policy_result is None:
                raise CalculationFailedError(
                    RunError(
                        RunErrorCode.INVALID_RESULT,
                        "The household outcome has no result for one of the "
                        "call's runs.",
                    )
                )
            outputs.append((run, _household_output(result, policy_result)))
        for run, output in outputs:
            self._write_output(connection, run, output)
