"""Recording the household job a v2 household function's call names.

The simulation service inserts each household job pending with no Modal
call, starts a household function with the job's ID, then records the call's
ID on the job. The function claims the job under its own Modal call ID,
calculates the household, and records the outcome on the job, as the Modal
worker role: a ``HouseholdExecutionResult`` as the job's result, or a
``HouseholdExecutionFailure`` as its result with a one-line summary as its
error. A job another call holds, or one already finished, is not calculated
again. A function given no job, as release validation calls it, touches no
database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionFailure,
)

from .failure_recording import persist_failure_record
from .report_run_access import report_run_store

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from uuid import UUID

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.household_execution import (
        HouseholdExecutionResult,
    )
    from policyengine_stage12_persistence import RunStatus, RunWrite

    from .report_run_access import HouseholdJobWriter

logger = logging.getLogger(__name__)

# A failed job's ``error_message`` is one line of at most this many
# characters; its result holds the whole failure.
MAXIMUM_FAILURE_SUMMARY_LENGTH = 500
_SHORTENED_SUFFIX = "..."


def household_failure_summary(failure: HouseholdExecutionFailure) -> str:
    """Return a failed calculation's one-line summary: its error code, then message.

    Whitespace in the message, line breaks included, becomes single spaces,
    and a summary longer than ``MAXIMUM_FAILURE_SUMMARY_LENGTH`` is shortened
    to end with an ellipsis.
    """
    error = failure.error
    summary = f"{error.code}: {' '.join(error.message.split())}"
    if len(summary) <= MAXIMUM_FAILURE_SUMMARY_LENGTH:
        return summary
    kept = MAXIMUM_FAILURE_SUMMARY_LENGTH - len(_SHORTENED_SUFFIX)
    return summary[:kept] + _SHORTENED_SUFFIX


def unexpected_failure_summary(error: BaseException) -> str:
    """Describe an error outside the calculation, or Modal's cancellation.

    The summary names only the error's type: its message may repeat the
    request, which the job already holds.
    """
    if not isinstance(error, Exception):
        return (
            "household_function_cancelled: Modal cancelled the household "
            "function's call."
        )
    return (
        "household_function_error: The household function raised "
        f"{type(error).__name__}."
    )


@dataclass(frozen=True)
class HouseholdJobRecorder:
    """A household function's writes to the job its call names, under its call ID."""

    writer: HouseholdJobWriter
    household_job_id: UUID
    modal_call_id: str

    def claim(self) -> RunWrite:
        """Mark the job running under this call, unless another call holds it."""
        return self.writer.claim_household_job(
            self.household_job_id,
            modal_call_id=self.modal_call_id,
            claimed_at=datetime.now(UTC),
        )

    def complete(self, result: Mapping[str, Any]) -> RunWrite:
        """Mark the job succeeded with its result document."""
        return self.writer.complete_household_job(
            self.household_job_id,
            modal_call_id=self.modal_call_id,
            result=result,
            completed_at=datetime.now(UTC),
        )

    def fail(
        self, error_message: str, *, result: Mapping[str, Any] | None = None
    ) -> RunWrite:
        """Mark the job failed with its summary and, if any, its failure document."""
        return self.writer.fail_household_job(
            self.household_job_id,
            modal_call_id=self.modal_call_id,
            error_message=error_message,
            failed_at=datetime.now(UTC),
            result=result,
        )

    def fail_quietly(
        self, error_message: str, *, runtime: ObservabilityRuntime | None
    ) -> None:
        """Fail the job, logging rather than raising a failed write."""
        persist_failure_record(
            partial(self.fail, error_message),
            runtime=runtime,
            failure_scope="household_job",
        )


def run_household_job(
    household_job_id: UUID,
    calculate: Callable[[], HouseholdExecutionResult | HouseholdExecutionFailure],
    *,
    modal_call_id: str | None,
    runtime: ObservabilityRuntime | None = None,
    writer: HouseholdJobWriter | None = None,
) -> dict[str, Any]:
    """Claim the job, calculate it with ``calculate``, and record the outcome.

    Returns the outcome as JSON, or, for a job another call holds or one
    already finished, a summary of the job without calculating. ``writer``
    defaults to the store the Modal worker role's database URL names. Once
    the call holds the job, an error or Modal's cancellation fails the job
    with a generic summary, then propagates; a pre-emption doesn't, because
    Modal runs the call again under the same call ID. The claim is treated
    the same way: it may have committed before the error, and failing a job
    this call doesn't hold changes nothing. Raises RuntimeError, before any
    write, when ``modal_call_id`` is None.
    """
    if modal_call_id is None:
        msg = "Modal household function call identifier is unavailable"
        raise RuntimeError(msg)
    recorder = HouseholdJobRecorder(
        writer=writer or report_run_store(runtime),
        household_job_id=household_job_id,
        modal_call_id=modal_call_id,
    )
    try:
        claimed = recorder.claim()
        if not claimed.changed:
            return _job_summary(household_job_id, claimed.status)
        outcome = calculate()
        document = outcome.model_dump(mode="json")
        if isinstance(outcome, HouseholdExecutionFailure):
            recorded = recorder.fail(
                household_failure_summary(outcome), result=document
            )
        else:
            recorded = recorder.complete(document)
    except KeyboardInterrupt:
        # Pre-emption: Modal runs the call again, and it resumes this claim.
        raise
    except BaseException as error:
        recorder.fail_quietly(unexpected_failure_summary(error), runtime=runtime)
        raise
    if not recorded.changed:
        logger.warning(
            "Household job %s was %s before its calculation was recorded",
            household_job_id,
            recorded.status.value,
        )
    return document


def _job_summary(household_job_id: UUID, status: RunStatus) -> dict[str, Any]:
    return {
        "deduplicated": True,
        "household_job_id": str(household_job_id),
        "status": status.value,
    }
