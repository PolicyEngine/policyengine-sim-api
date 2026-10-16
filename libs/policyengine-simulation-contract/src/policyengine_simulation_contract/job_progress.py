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

from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    NonNegativeFloat,
    TypeAdapter,
    ValidationError,
    model_validator,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

JOB_PROGRESS_CAPABILITY_KEY = "job_progress"
JOB_PROGRESS_CAPABILITY_VERSION = 1


class JobStage(StrEnum):
    """Coarse stages of an annual economy job, in the order they run.

    A monolithic run moves through ``loading_data``, ``baseline``, ``reform``
    and ``outputs``. A segmented US national run computes baseline and reform
    together in parallel region segments, so it moves through
    ``loading_data``, ``simulating`` and ``outputs``.

    Later versions may add stages. Clients must accept a value they do not
    know, for example by showing a generic "running" state.
    """

    QUEUED = "queued"
    LOADING_DATA = "loading_data"
    BASELINE = "baseline"
    REFORM = "reform"
    SIMULATING = "simulating"
    OUTPUTS = "outputs"


class JobExecution(StrEnum):
    """How the worker runs the job; the two paths spend time differently."""

    MONOLITHIC = "monolithic"
    SEGMENTED = "segmented"


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
    # None only between the worker start and its choice of execution path.
    execution: JobExecution | None = None
    # Modal can run the same call again after a preemption; each run of the
    # call is one attempt, and later attempts continue the same record.
    attempts: int = Field(default=1, ge=1)
    segments: JobSegmentProgress | None = None
    stage_durations: dict[JobStage, NonNegativeFloat] = Field(default_factory=dict)


# Percent of submission-to-completion time that has passed, in a typical
# successful production run, when each stage starts. Set from one week of
# production telemetry (6-13 October 2026); queue time counts towards the
# ``loading_data`` floor. PROVISIONAL: replace with stage shares measured per
# country and region type once runtime statistics exist (#780, phase 2).
#
# Monolithic: UK national and US state runs. The baseline/reform split comes
# from Stage 12 state runs (baseline is about 29% of their calculation).
# Segmented: US national runs; the national reduce is short. Completed
# segments do not move progress: the regional children finish at about the
# same time and California finishes last, so a count jumps late and then
# waits. ``segments`` reports the count for captions instead.
STAGE_PROGRESS_FLOOR: Mapping[JobExecution, Mapping[JobStage, int]] = MappingProxyType(
    {
        JobExecution.MONOLITHIC: MappingProxyType(
            {
                JobStage.QUEUED: 0,
                JobStage.LOADING_DATA: 25,
                JobStage.BASELINE: 55,
                JobStage.REFORM: 67,
                JobStage.OUTPUTS: 97,
            }
        ),
        JobExecution.SEGMENTED: MappingProxyType(
            {
                JobStage.QUEUED: 0,
                JobStage.LOADING_DATA: 3,
                JobStage.SIMULATING: 5,
                JobStage.OUTPUTS: 97,
            }
        ),
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


def stage_progress_floor(execution: JobExecution | None, stage: JobStage) -> int:
    """The progress percent at which ``stage`` starts on ``execution``.

    Before the worker chooses its path, use the lowest floor that any path
    gives the stage, so progress never moves backwards once the path is known.
    """

    floors = STAGE_PROGRESS_FLOOR.get(execution) if execution is not None else None
    if floors is not None and stage in floors:
        return floors[stage]
    return min(
        (floors[stage] for floors in STAGE_PROGRESS_FLOOR.values() if stage in floors),
        default=0,
    )


def stage_progress_max(execution: JobExecution | None, stage: JobStage) -> int:
    """The highest progress percent a job reaches before its next stage.

    This is one below the next stage's floor, or ``RUNNING_PROGRESS_CAP`` for
    the last stage. Before the worker chooses its path, use the highest value
    that any path gives the stage, so a client estimate is not held back.
    """

    paths = [STAGE_PROGRESS_FLOOR[execution]] if execution is not None else []
    if not any(stage in floors for floors in paths):
        paths = list(STAGE_PROGRESS_FLOOR.values())
    limits = []
    for floors in paths:
        stages = list(floors)
        if stage not in floors:
            continue
        index = stages.index(stage)
        if index + 1 < len(stages):
            limits.append(floors[stages[index + 1]] - 1)
        else:
            limits.append(RUNNING_PROGRESS_CAP)
    floor = stage_progress_floor(execution, stage)
    return min(max([floor, *limits]), RUNNING_PROGRESS_CAP)


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
                fields["progress"] = stage_progress_floor(None, JobStage.QUEUED)
                fields["progress_max"] = stage_progress_max(None, JobStage.QUEUED)
        else:
            fields["started_at"] = _json_datetime(record.started_at)
            fields["finished_at"] = _json_datetime(record.finished_at)
            if status != "complete":
                fields["stage"] = record.stage.value
                fields["stage_started_at"] = _json_datetime(record.stage_started_at)
            if status == "running":
                fields["progress"] = min(
                    stage_progress_floor(record.execution, record.stage),
                    RUNNING_PROGRESS_CAP,
                )
                fields["progress_max"] = stage_progress_max(
                    record.execution, record.stage
                )
            if record.segments is not None:
                fields["segments"] = record.segments.model_dump(mode="json")
            if record.stage_durations:
                fields["stage_durations"] = {
                    stage.value: seconds
                    for stage, seconds in record.stage_durations.items()
                }
    return {key: value for key, value in fields.items() if value is not None}
