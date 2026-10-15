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
    """Stage boundaries the runtime reports while an annual job runs."""

    def start(self) -> None:
        """Begin the job at the ``loading_data`` stage."""
        ...

    def set_execution(self, execution: JobExecution) -> None:
        """Record whether the job runs as one calculation or in segments."""
        ...

    def enter(self, stage: JobStage) -> None:
        """Close the current stage and begin ``stage``."""
        ...

    def start_segments(self, total: int) -> None:
        """Enter the ``simulating`` stage with ``total`` region segments to run."""
        ...

    def segment_completed(self) -> None:
        """Count one more finished region segment."""
        ...

    def finish(self) -> None:
        """Close the current stage after the job succeeds or fails."""
        ...

    def interrupt(self) -> None:
        """Close the current stage but leave the job running for a retry."""
        ...

    def stage_durations(self) -> dict[JobStage, float]:
        """Return the seconds spent in each ended stage."""
        ...

    def attempts(self) -> int:
        """Return how many times Modal has started this call."""
        ...


class NullJobProgressReporter:
    """Reports nothing."""

    def start(self) -> None:
        """Do nothing; no record is kept."""

    def set_execution(self, execution: JobExecution) -> None:
        """Discard the execution path."""

    def enter(self, stage: JobStage) -> None:
        """Discard the stage."""

    def start_segments(self, total: int) -> None:
        """Discard the segment count."""

    def segment_completed(self) -> None:
        """Discard the finished segment."""

    def finish(self) -> None:
        """Do nothing; there is no stage to close."""

    def interrupt(self) -> None:
        """Do nothing; there is no stage to close."""

    def stage_durations(self) -> dict[JobStage, float]:
        """Return no durations, since no stage is timed."""
        return {}

    def attempts(self) -> int:
        """Return 1, since no earlier attempt is known."""
        return 1


NULL_JOB_PROGRESS = NullJobProgressReporter()


class StoredJobProgressReporter:
    """Writes the job's ``JobProgressRecord`` after every change.

    Modal can run the same call again after a preemption, under the same
    call id. ``start()`` therefore continues any record that an earlier
    attempt wrote: ``started_at`` and the ended-stage durations carry over,
    ``attempts`` counts up, and the published stage stays at the earlier
    attempt's stage until this attempt reaches a stage that starts at least
    as far along. A preempted attempt calls ``interrupt()`` rather than
    ``finish()``, so its record stays running and keeps any held stage for
    the next attempt. ``progress`` therefore never moves backwards, however
    many times the call is preempted.
    """

    def __init__(
        self,
        job_id: str,
        *,
        write: ProgressWriter,
        read: ProgressReader | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """Report ``job_id``'s stages through ``write``, timed by ``clock``.

        With ``read``, ``start()`` continues the record that an earlier
        attempt of the same call wrote.
        """
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
        self._interrupted = False

    @property
    def record(self) -> JobProgressRecord | None:
        """Return this attempt's record, without any earlier attempt's held stage."""
        return self._record

    def start(self) -> None:
        """Write the first record, continuing an earlier attempt's if there is one.

        Calls after the first do nothing.
        """
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
        """Record the execution path, starting the record first if needed."""
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
        """Close the current stage and begin ``stage``, starting the record first.

        Entering the current stage again, or any stage after ``finish()``,
        changes nothing.
        """
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
        """Enter ``simulating`` and record ``total`` segments, none completed.

        A total below 1 records no segment count.
        """
        self.enter(JobStage.SIMULATING)
        record = self._record
        if record is None or record.finished_at is not None or total < 1:
            return
        record.segments = JobSegmentProgress(completed=0, total=total)
        self._publish()

    def segment_completed(self) -> None:
        """Count one more completed segment, never beyond the total."""
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
        if record is None or record.finished_at is not None or self._interrupted:
            return
        record.finished_at = self._close_stage(record)
        # A failed job reports the stage this attempt stopped in.
        self._held = None
        self._publish()

    def interrupt(self) -> None:
        """Leave the record running for the call's next attempt.

        Modal preempts a call with SIGINT, which a sync function receives as
        ``KeyboardInterrupt``, and then runs the call again under the same
        call id. The current stage's time still counts in the durations, but
        the record does not finish and any held stage stays held, so the next
        attempt continues from the furthest stage reached. ``finish()`` does
        nothing afterwards.
        """
        record = self._record
        if record is None or record.finished_at is not None or self._interrupted:
            return
        self._interrupted = True
        self._close_stage(record)
        self._publish()

    def stage_durations(self) -> dict[JobStage, float]:
        """Seconds per ended stage over all attempts; complete after ``finish()``."""
        if self._record is None:
            return {}
        return dict(self._record.stage_durations)

    def attempts(self) -> int:
        """Return this attempt's number, counting from 1."""
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
        """Return the record clients see, never behind an earlier attempt's stage."""
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
