"""Record the stage of an annual job for clients that poll its status.

The runtime calls a ``JobProgressReporter`` at stage boundaries. The
gateway-facing Modal function creates a ``StoredJobProgressReporter`` for its
own job; every other caller (segment children, Stage 12, tests) gets the
no-op ``NULL_JOB_PROGRESS``.

Stage reporting is diagnostic for clients and must never change a
calculation: every read and write failure is caught and logged, and the job
goes on.
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
    stage_progress_floor,
)

logger = logging.getLogger(__name__)

ProgressWriter = Callable[[str, JobProgressRecord], None]
ProgressReader = Callable[[str], JobProgressRecord | None]


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

    def attempts(self) -> int: ...


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

    def attempts(self) -> int:
        return 1


NULL_JOB_PROGRESS = NullJobProgressReporter()


class StoredJobProgressReporter:
    """Writes the job's ``JobProgressRecord`` after every change.

    Modal can run the same call again after a preemption, under the same
    call id. ``start()`` therefore continues any record that an earlier
    attempt wrote: ``started_at`` and the ended-stage durations carry over,
    ``attempts`` counts up, and the published stage stays at the earlier
    attempt's stage until this attempt reaches a stage that starts at least
    as far along. ``progress`` therefore never moves backwards.
    """

    def __init__(
        self,
        job_id: str,
        *,
        write: ProgressWriter,
        read: ProgressReader | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.job_id = job_id
        self._write = write
        self._read = read
        self._clock = clock
        # This attempt's own state; stage durations come from it.
        self._record: JobProgressRecord | None = None
        # The stage and its start that an earlier attempt had reached.
        self._held: tuple[JobStage, datetime] | None = None
        self._read_failed = False
        self._write_failed = False

    @property
    def record(self) -> JobProgressRecord | None:
        return self._record

    def start(self) -> None:
        if self._record is not None:
            return
        now = self._clock()
        earlier = self._earlier_attempt()
        if earlier is None:
            self._record = JobProgressRecord(
                stage=JobStage.LOADING_DATA,
                started_at=now,
                stage_started_at=now,
            )
        else:
            self._record = JobProgressRecord(
                stage=JobStage.LOADING_DATA,
                started_at=earlier.started_at,
                stage_started_at=now,
                execution=earlier.execution,
                stage_durations=dict(earlier.stage_durations),
                attempts=earlier.attempts + 1,
            )
            self._held = (earlier.stage, earlier.stage_started_at)
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
        # A failed job reports the stage this attempt stopped in.
        self._held = None
        self._publish()

    def stage_durations(self) -> dict[JobStage, float]:
        """Seconds per ended stage over all attempts; complete after ``finish()``."""

        if self._record is None:
            return {}
        return dict(self._record.stage_durations)

    def attempts(self) -> int:
        return 1 if self._record is None else self._record.attempts

    def _earlier_attempt(self) -> JobProgressRecord | None:
        if self._read is None:
            return None
        try:
            return self._read(self.job_id)
        except Exception as exc:  # noqa: BLE001 - stage reporting is non-fatal
            if not self._read_failed:
                logger.warning(
                    "Could not read job progress for %s: %s",
                    self.job_id,
                    type(exc).__name__,
                )
            self._read_failed = True
            return None

    def _published(self, record: JobProgressRecord) -> JobProgressRecord:
        """The record clients see: never behind an earlier attempt's stage."""

        snapshot = record.model_copy(deep=True)
        if self._held is None:
            return snapshot
        held_stage, held_started_at = self._held
        if stage_progress_floor(record.execution, record.stage) >= (
            stage_progress_floor(record.execution, held_stage)
        ):
            self._held = None
            return snapshot
        snapshot.stage = held_stage
        snapshot.stage_started_at = held_started_at
        return snapshot

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
            self._write(self.job_id, self._published(self._record))
        except Exception as exc:  # noqa: BLE001 - stage reporting is non-fatal
            if not self._write_failed:
                logger.warning(
                    "Could not record job progress for %s: %s",
                    self.job_id,
                    type(exc).__name__,
                )
            self._write_failed = True
