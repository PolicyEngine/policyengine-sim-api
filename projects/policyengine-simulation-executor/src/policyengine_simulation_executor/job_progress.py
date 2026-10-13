"""Record the stage of an annual job for clients that poll its status.

The runtime calls a ``JobProgressReporter`` at stage boundaries. The
gateway-facing Modal function creates a ``StoredJobProgressReporter`` for its
own job; every other caller (segment children, Stage 12, tests) gets the
no-op ``NULL_JOB_PROGRESS``.

Stage reporting is diagnostic for clients and must never change a
calculation: every write failure is caught and logged, and the job goes on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from policyengine_simulation_contract.job_progress import (
    JobExecution,
    JobProgressRecord,
    JobSegmentProgress,
    JobStage,
)

logger = logging.getLogger(__name__)

ProgressWriter = Callable[[str, JobProgressRecord], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class JobProgressReporter(Protocol):
    def start(self) -> None: ...

    def set_execution(self, execution: JobExecution) -> None: ...

    def enter(self, stage: JobStage) -> None: ...

    def start_segments(self, total: int) -> None: ...

    def segment_completed(self) -> None: ...

    def finish(self) -> None: ...

    def stage_durations(self) -> dict[JobStage, float]: ...


class NullJobProgressReporter:
    """Reports nothing."""

    def start(self) -> None:
        pass

    def set_execution(self, execution: JobExecution) -> None:
        pass

    def enter(self, stage: JobStage) -> None:
        pass

    def start_segments(self, total: int) -> None:
        pass

    def segment_completed(self) -> None:
        pass

    def finish(self) -> None:
        pass

    def stage_durations(self) -> dict[JobStage, float]:
        return {}


NULL_JOB_PROGRESS = NullJobProgressReporter()


class StoredJobProgressReporter:
    """Writes the job's ``JobProgressRecord`` after every change."""

    def __init__(
        self,
        job_id: str,
        *,
        write: ProgressWriter,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.job_id = job_id
        self._write = write
        self._clock = clock
        self._record: JobProgressRecord | None = None
        self._write_failed = False

    @property
    def record(self) -> JobProgressRecord | None:
        return self._record

    def start(self) -> None:
        if self._record is not None:
            return
        now = self._clock()
        self._record = JobProgressRecord(
            stage=JobStage.LOADING_DATA,
            started_at=now,
            stage_started_at=now,
        )
        self._publish()

    def set_execution(self, execution: JobExecution) -> None:
        if self._record is None:
            self.start()
        record = self._record
        if record is None or record.finished_at is not None:
            return
        if record.execution is execution:
            return
        record.execution = execution
        self._publish()

    def enter(self, stage: JobStage) -> None:
        if self._record is None:
            self.start()
        record = self._record
        if record is None or record.finished_at is not None:
            return
        if record.stage is stage:
            return
        now = self._close_stage(record)
        record.stage = stage
        record.stage_started_at = now
        self._publish()

    def start_segments(self, total: int) -> None:
        self.enter(JobStage.SIMULATING)
        record = self._record
        if record is None or record.finished_at is not None or total < 1:
            return
        record.segments = JobSegmentProgress(completed=0, total=total)
        self._publish()

    def segment_completed(self) -> None:
        record = self._record
        if record is None or record.finished_at is not None:
            return
        segments = record.segments
        if segments is None or segments.completed >= segments.total:
            return
        record.segments = JobSegmentProgress(
            completed=segments.completed + 1,
            total=segments.total,
        )
        self._publish()

    def finish(self) -> None:
        """Close the current stage. Runs after success and after failure."""

        record = self._record
        if record is None or record.finished_at is not None:
            return
        record.finished_at = self._close_stage(record)
        self._publish()

    def stage_durations(self) -> dict[JobStage, float]:
        """Seconds per ended stage; complete only after ``finish()``."""

        if self._record is None:
            return {}
        return dict(self._record.stage_durations)

    def _close_stage(self, record: JobProgressRecord) -> datetime:
        now = self._clock()
        elapsed = max((now - record.stage_started_at).total_seconds(), 0.0)
        previous = record.stage_durations.get(record.stage, 0.0)
        record.stage_durations[record.stage] = round(previous + elapsed, 2)
        return now

    def _publish(self) -> None:
        if self._record is None:
            return
        try:
            self._write(self.job_id, self._record.model_copy(deep=True))
        except Exception as exc:  # noqa: BLE001 - stage reporting is non-fatal
            if not self._write_failed:
                logger.warning(
                    "Could not record job progress for %s: %s",
                    self.job_id,
                    type(exc).__name__,
                )
            self._write_failed = True
