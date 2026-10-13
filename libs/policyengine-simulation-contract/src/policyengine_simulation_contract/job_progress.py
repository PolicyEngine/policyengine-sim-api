"""Client-visible stage and progress of an annual economy job.

The worker that runs a job records which stage it is in. The gateway reads
that record on every status poll and adds the optional stage fields to the
``JobStatusResponse``. Clients that read only ``status`` see no change.

Records are keyed by the job's Modal function-call id, which is the public
``job_id``. Older worker apps do not write records, so the gateway reports a
stage only for jobs routed to a worker whose bundle manifest declares
``JOB_PROGRESS_CAPABILITY_VERSION``. Without that declaration a missing
record would be indistinguishable from a job that has not started.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    NonNegativeFloat,
    TypeAdapter,
    ValidationError,
    model_validator,
)

JOB_PROGRESS_CAPABILITY_KEY = "job_progress"
JOB_PROGRESS_CAPABILITY_VERSION = 1


class JobStage(StrEnum):
    """Coarse stages of an annual economy job, in the order they run.

    A monolithic run moves through ``loading_data``, ``baseline``, ``reform``
    and ``outputs``. A segmented US national run computes baseline and reform
    together in parallel region segments, so it moves through
    ``loading_data``, ``simulating`` and ``outputs``.
    """

    QUEUED = "queued"
    LOADING_DATA = "loading_data"
    BASELINE = "baseline"
    REFORM = "reform"
    SIMULATING = "simulating"
    OUTPUTS = "outputs"


class JobSegmentProgress(BaseModel):
    """Completed region segments of a segmented national run."""

    completed: int = Field(ge=0)
    total: int = Field(ge=1)

    @model_validator(mode="after")
    def _completed_within_total(self) -> JobSegmentProgress:
        if self.completed > self.total:
            raise ValueError("completed segments cannot exceed total segments")
        return self


class JobProgressRecord(BaseModel):
    """Worker-owned stage state of one job. Internal; not a public schema."""

    schema_version: Literal[1] = 1
    stage: JobStage
    started_at: AwareDatetime
    stage_started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    segments: JobSegmentProgress | None = None
    stage_durations: dict[JobStage, NonNegativeFloat] = Field(default_factory=dict)


# Percent of a typical run that is complete when each stage starts.
# PROVISIONAL: chosen by hand, not measured. Replace these with measured
# stage shares once per-stage runtime statistics exist (#780, phase 2).
# ``simulating`` advances from its floor to the ``outputs`` floor as
# segments complete.
STAGE_PROGRESS_FLOOR: Mapping[JobStage, int] = MappingProxyType(
    {
        JobStage.QUEUED: 0,
        JobStage.LOADING_DATA: 5,
        JobStage.BASELINE: 15,
        JobStage.SIMULATING: 15,
        JobStage.REFORM: 45,
        JobStage.OUTPUTS: 85,
    }
)
# A running job never reports 100: only a complete result does.
RUNNING_PROGRESS_CAP = 99

JobStatus = Literal["running", "complete", "failed"]

_DATETIME = TypeAdapter(AwareDatetime)


def supported_job_progress_capability(value: object) -> int | None:
    """Return the declared capability version when this gateway reads it."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value != JOB_PROGRESS_CAPABILITY_VERSION:
        return None
    return value


def _json_datetime(value: datetime | None) -> str | None:
    return None if value is None else _DATETIME.dump_python(value, mode="json")


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return _DATETIME.validate_python(value)
    except ValidationError:
        return None


def _running_progress(record: JobProgressRecord) -> int:
    floor = STAGE_PROGRESS_FLOOR[record.stage]
    if record.stage is JobStage.SIMULATING and record.segments is not None:
        ceiling = STAGE_PROGRESS_FLOOR[JobStage.OUTPUTS]
        share = record.segments.completed / record.segments.total
        floor += int((ceiling - floor) * share)
    return min(floor, RUNNING_PROGRESS_CAP)


def job_progress_fields(
    *,
    status: JobStatus,
    submitted_at: object,
    record: JobProgressRecord | None,
    reports_stages: bool,
) -> dict[str, Any]:
    """Build the optional stage fields of a job status response.

    Returns JSON-ready values and omits every field that has no value, so a
    job without stage reporting keeps its existing response body.

    ``reports_stages`` is true only when the job's worker declares the stage
    capability and the gateway read its record (or found none) without an
    error. Then a running job without a record is ``queued``.
    """

    fields: dict[str, Any] = {
        "submitted_at": _json_datetime(_parse_datetime(submitted_at))
    }
    if reports_stages:
        if status == "complete":
            fields["progress"] = 100
        if record is None:
            if status == "running":
                fields["stage"] = JobStage.QUEUED.value
                fields["progress"] = STAGE_PROGRESS_FLOOR[JobStage.QUEUED]
        else:
            fields["started_at"] = _json_datetime(record.started_at)
            fields["finished_at"] = _json_datetime(record.finished_at)
            if status != "complete":
                fields["stage"] = record.stage.value
                fields["stage_started_at"] = _json_datetime(record.stage_started_at)
            if status == "running":
                fields["progress"] = _running_progress(record)
            if record.segments is not None:
                fields["segments"] = record.segments.model_dump(mode="json")
            if record.stage_durations:
                fields["stage_durations"] = {
                    stage.value: seconds
                    for stage, seconds in record.stage_durations.items()
                }
    return {key: value for key, value in fields.items() if value is not None}
