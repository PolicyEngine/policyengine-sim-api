"""The report a Modal call sends the Simulation Entrypoint when it ends.

The last step of each top-level v2 Modal function (the economy coordinator
and the household functions) reports that its call ended: it starts the small
Modal function ``CALL_REPORT_FUNCTION_NAME``, which posts a
``CallEndedReport`` to the entrypoint's ``CALL_ENDED_PATH`` with the Modal
identity token of its container as a bearer token. The report carries the
call ID and the run IDs only, never a result: the entrypoint checks that the
request comes from that Modal function, asks Modal for the call's outcome
itself, and records it.

The entrypoint answers with a ``CallReportAnswer``. While it cannot record
the outcome yet (the call has not finished, its ID is not stored yet, Modal
or the database cannot be reached), it answers 503 with a status in
``RETRY_LATER_STATUSES``, and the sender reports again after a wait. A
repeated report changes nothing.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The entrypoint's route for call-ended reports. It is not in the public
# OpenAPI document.
CALL_ENDED_PATH = "/internal/modal-calls/ended"
# The Modal function that sends reports; the entrypoint accepts identity
# tokens of this function only.
CALL_REPORT_FUNCTION_NAME = "report_call_ended"
# Modal sets this variable to the container's identity token in every
# function container.
MODAL_IDENTITY_ENVIRONMENT_VARIABLE = "MODAL_IDENTITY_TOKEN"
# A Modal function call ID, as Modal prints it.
MODAL_CALL_ID_PATTERN = r"^fc-[A-Za-z0-9]{1,64}$"
# A call computes one simulation run, or a household report's baseline and
# reform runs together.
MOST_SIMULATION_RUNS_PER_CALL = 2


class CallEndedReport(BaseModel):
    """A Modal call's report that it ended: its ID and the runs it computes.

    Exactly one of ``simulation_run_ids`` (a simulation call: one run, or a
    household report's two) and ``report_run_id`` (an economy comparison
    step's call) is set.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    modal_call_id: str = Field(pattern=MODAL_CALL_ID_PATTERN)
    simulation_run_ids: tuple[UUID, ...] | None = Field(
        default=None, min_length=1, max_length=MOST_SIMULATION_RUNS_PER_CALL
    )
    report_run_id: UUID | None = None

    @model_validator(mode="after")
    def _names_one_kind_of_run(self) -> Self:
        if (self.simulation_run_ids is None) == (self.report_run_id is None):
            msg = "name either simulation_run_ids or report_run_id"
            raise ValueError(msg)
        if self.simulation_run_ids is not None and len(
            set(self.simulation_run_ids)
        ) != len(self.simulation_run_ids):
            msg = "simulation_run_ids names a run twice"
            raise ValueError(msg)
        return self


class CallReportStatus(StrEnum):
    """What the entrypoint did with a report."""

    # The call's outcome is now recorded on its runs.
    RECORDED = "recorded"
    # The runs already hold the call's outcome; nothing changed.
    ALREADY_RECORDED = "already_recorded"
    # The runs are still being dispatched and do not hold the call ID yet.
    NOT_DISPATCHED_YET = "not_dispatched_yet"
    # Modal holds no output for the call yet.
    STILL_RUNNING = "still_running"
    # Modal could not be asked about the call.
    MODAL_UNREACHABLE = "modal_unreachable"
    # The serving revision cannot record this kind of result yet.
    NO_RESULT_RECORDER = "no_result_recorder"
    # The database could not be read or written.
    NOT_WRITTEN = "not_written"
    # The entrypoint is not configured to accept reports.
    NOT_CONFIGURED = "not_configured"
    # No run, or not every run, the report names exists.
    UNKNOWN_RUNS = "unknown_runs"
    # The runs hold another Modal call, or finished without holding one (for
    # example failed as abandoned before the call's ID was stored).
    OTHER_CALL = "other_call"


RETRY_LATER_STATUSES = frozenset(
    {
        CallReportStatus.NOT_DISPATCHED_YET,
        CallReportStatus.STILL_RUNNING,
        CallReportStatus.MODAL_UNREACHABLE,
        CallReportStatus.NO_RESULT_RECORDER,
        CallReportStatus.NOT_WRITTEN,
        CallReportStatus.NOT_CONFIGURED,
    }
)


class CallReportAnswer(BaseModel):
    """The entrypoint's answer to a report."""

    model_config = ConfigDict(frozen=True)

    status: CallReportStatus

    @property
    def retry_later(self) -> bool:
        """Whether the sender should report again after a wait."""
        return self.status in RETRY_LATER_STATUSES
