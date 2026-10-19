"""Fakes for the simulation job service: store, Modal calls and Cloud Tasks.

``InMemoryJobStore`` keeps the real store's contract: every transition is
conditional on the current status and applied under one lock, so a second
writer finds nothing to change. The real conditional SQL is tested against
PostgreSQL in libs/policyengine-simulation-persistence.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_entry.simulation_jobs import (
    CallRunning,
    CollectionQueueError,
    JobKind,
    ModalTarget,
)
from policyengine_simulation_persistence.jobs import (
    DISPATCH_ABANDONED,
    UNFINISHED_STATUSES,
    JobRecord,
    JobResultRecord,
    JobStatus,
    Submission,
    result_sha256,
)
from policyengine_simulation_persistence.reports import (
    ReportKind,
    ReportResultRecord,
    report_result_values,
)
from pydantic import BaseModel, ConfigDict

START = datetime(2026, 10, 20, 12, tzinfo=UTC)
TARGET = ModalTarget(
    application="policyengine-simulation-v2-py6-7-5",
    function="calculate_household",
)


class HouseholdResult(BaseModel):
    """A stand-in result model for a registered job kind."""

    model_config = ConfigDict(extra="forbid")

    net_income: float


HOUSEHOLD_KIND = JobKind(
    name="household_calculation",
    result_model=HouseholdResult,
    result_schema_version=1,
)


ECONOMY_REPORT_KIND = JobKind(
    name="economy_report",
    result_model=SingleYearMacroOutput,
    result_schema_version=1,
    report_kind=ReportKind.ECONOMY,
)


class SteppingClock:
    """A clock the test moves forward explicitly."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **elapsed: float) -> None:
        self.now += timedelta(**elapsed)


class InMemoryJobStore:
    """The JobStore contract in memory, with conditional transitions."""

    def __init__(self, clock: SteppingClock) -> None:
        self.clock = clock
        self.jobs: dict[UUID, JobRecord] = {}
        self.results: dict[UUID, JobResultRecord] = {}
        self.report_results: dict[UUID, ReportResultRecord] = {}
        self.lock = threading.Lock()
        self.calls: list[str] = []

    def _transition(self, job_id, expected, **changes) -> JobRecord | None:
        job = self.jobs.get(job_id)
        if job is None or job.status not in expected:
            return None
        updated = replace(job, updated_at=self.clock(), **changes)
        self.jobs[job_id] = updated
        return updated

    def submit(self, new_job):
        self.calls.append("submit")
        with self.lock:
            for job in self.jobs.values():
                if (
                    job.input_sha256 == new_job.input_sha256
                    and job.status is not JobStatus.FAILED
                ):
                    return Submission(job=job, created=False)
            now = self.clock()
            job = JobRecord(
                id=uuid4(),
                kind=new_job.kind,
                status=JobStatus.PENDING,
                submitted_by=new_job.submitted_by,
                input_sha256=new_job.input_sha256,
                policyengine_version=new_job.policyengine_version,
                modal_application=new_job.modal_application,
                modal_function=new_job.modal_function,
                modal_call_id=None,
                observability_id=new_job.observability_id,
                error_code=None,
                error_message=None,
                collection_attempts=0,
                created_at=now,
                updated_at=now,
                dispatched_at=None,
                last_checked_at=None,
                completed_at=None,
            )
            self.jobs[job.id] = job
            return Submission(job=job, created=True)

    def claim_dispatch(self, job_id):
        self.calls.append("claim_dispatch")
        with self.lock:
            return (
                self._transition(
                    job_id, {JobStatus.PENDING}, status=JobStatus.DISPATCHING
                )
                is not None
            )

    def record_dispatched(self, job_id, *, modal_call_id):
        self.calls.append("record_dispatched")
        with self.lock:
            return (
                self._transition(
                    job_id,
                    {JobStatus.DISPATCHING},
                    status=JobStatus.RUNNING,
                    modal_call_id=modal_call_id,
                    dispatched_at=self.clock(),
                )
                is not None
            )

    def record_dispatch_failure(self, job_id, *, error_code, error_message):
        self.calls.append("record_dispatch_failure")
        with self.lock:
            return (
                self._transition(
                    job_id,
                    {JobStatus.DISPATCHING},
                    status=JobStatus.FAILED,
                    error_code=error_code,
                    error_message=error_message,
                    completed_at=self.clock(),
                )
                is not None
            )

    def get(self, job_id):
        self.calls.append("get")
        return self.jobs.get(job_id)

    def get_by_modal_call_id(self, modal_call_id):
        self.calls.append("get_by_modal_call_id")
        return next(
            (job for job in self.jobs.values() if job.modal_call_id == modal_call_id),
            None,
        )

    def get_result(self, job_id):
        self.calls.append("get_result")
        return self.results.get(job_id)

    def record_check(self, job_id):
        self.calls.append("record_check")
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.status is not JobStatus.RUNNING:
                return None
            updated = self._transition(
                job_id,
                {JobStatus.RUNNING},
                collection_attempts=job.collection_attempts + 1,
                last_checked_at=self.clock(),
            )
            return updated.collection_attempts

    def complete(self, job_id, *, result, result_schema_version):
        self.calls.append("complete")
        with self.lock:
            finished = self._transition(
                job_id,
                {JobStatus.RUNNING},
                status=JobStatus.SUCCEEDED,
                completed_at=self.clock(),
            )
            if finished is None:
                return False
            self.results[job_id] = JobResultRecord(
                job_id=job_id,
                result=result,
                result_schema_version=result_schema_version,
                result_sha256=result_sha256(result),
                created_at=self.clock(),
            )
            return True

    def complete_report(
        self,
        job_id,
        *,
        report_kind,
        aggregate,
        aggregate_schema_version,
        artifacts=(),
    ):
        self.calls.append("complete_report")
        values = report_result_values(
            report_kind=report_kind,
            aggregate=aggregate,
            aggregate_schema_version=aggregate_schema_version,
            artifacts=artifacts,
        )
        with self.lock:
            finished = self._transition(
                job_id,
                {JobStatus.RUNNING},
                status=JobStatus.SUCCEEDED,
                completed_at=self.clock(),
            )
            if finished is None:
                return False
            self.report_results[job_id] = ReportResultRecord(
                job_id=job_id,
                created_at=self.clock(),
                **{
                    **values,
                    "report_kind": report_kind,
                    "artifacts": tuple(artifacts),
                },
            )
            return True

    def get_report_result(self, job_id):
        self.calls.append("get_report_result")
        return self.report_results.get(job_id)

    def fail(self, job_id, *, error_code, error_message):
        self.calls.append("fail")
        with self.lock:
            return (
                self._transition(
                    job_id,
                    {JobStatus.RUNNING},
                    status=JobStatus.FAILED,
                    error_code=error_code,
                    error_message=error_message,
                    completed_at=self.clock(),
                )
                is not None
            )

    def jobs_to_collect(self, *, checked_before, limit):
        self.calls.append("jobs_to_collect")
        candidates = [
            job
            for job in self.jobs.values()
            if job.status is JobStatus.RUNNING
            and (job.last_checked_at or job.dispatched_at or job.updated_at)
            < checked_before
        ]
        return [job.id for job in candidates[:limit]]

    def abandon_stalled_dispatches(self, *, updated_before):
        self.calls.append("abandon_stalled_dispatches")
        with self.lock:
            abandoned = []
            for job in list(self.jobs.values()):
                if (
                    job.status in UNFINISHED_STATUSES
                    and job.status is not JobStatus.RUNNING
                    and job.updated_at < updated_before
                ):
                    self._transition(
                        job.id,
                        {job.status},
                        status=JobStatus.FAILED,
                        error_code=DISPATCH_ABANDONED,
                        error_message="abandoned",
                        completed_at=self.clock(),
                    )
                    abandoned.append(job.id)
            return abandoned


class FakeDispatcher:
    """Acknowledge each spawn with a new call ID, or fail like Modal."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.spawned: list[tuple[ModalTarget, dict, dict | None]] = []

    async def spawn(self, target, request_payload, *, observability_context):
        self.spawned.append((target, request_payload, observability_context))
        if self.failure is not None:
            raise self.failure
        return f"fc-{len(self.spawned):026d}"


class FakeInspector:
    """Return a scripted outcome for each Modal call, or raise."""

    def __init__(self, outcome=None, failure: Exception | None = None) -> None:
        self.outcome = outcome if outcome is not None else CallRunning()
        self.failure = failure
        self.inspected: list[str] = []

    async def inspect(self, modal_call_id):
        self.inspected.append(modal_call_id)
        if self.failure is not None:
            raise self.failure
        return self.outcome


class FakeQueue:
    """Record each collection task, or refuse like Cloud Tasks."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.refuse = refuse
        self.enqueued: list[tuple[UUID, float]] = []

    async def enqueue(self, job_id, *, delay_seconds):
        if self.refuse:
            msg = "Cloud Tasks refused the task with HTTP 403"
            raise CollectionQueueError(msg)
        self.enqueued.append((job_id, delay_seconds))
