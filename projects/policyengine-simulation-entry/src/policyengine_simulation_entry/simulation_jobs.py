"""Simulation jobs: idempotent submission and background result collection.

The Simulation Entrypoint records one ``simulation.jobs`` row per
calculation, starts its Modal call once, and collects the result in the
background:

1. A submission inserts the job (identical inputs share one job), moves it
   from ``pending`` to ``dispatching`` with a conditional update, spawns the
   Modal call, records the call ID and schedules a Cloud Tasks collection task.
2. Each collection task checks the Modal call without waiting. A running call
   schedules the next task with exponential backoff; a finished call's result
   is validated and written with a conditional update, so a second collector
   changes nothing. No lease is taken.
3. A Cloud Scheduler sweep enqueues a task for running jobs that nobody has
   checked recently, and fails jobs whose dispatch stalled.

Status reads use only the database. Modal never receives database
credentials and never writes these tables.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

import google.auth
import google.auth.exceptions
import google.auth.transport.requests
import httpx
import modal
from policyengine_simulation_persistence.jobs import (
    JobRecord,
    JobResultRecord,
    JobStatus,
    NewJob,
    Submission,
    input_sha256,
    utc_now,
)
from policyengine_simulation_persistence.reports import (
    ReportKind,
    ReportResultRecord,
    UnsupportedReportAggregateError,
)
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from datetime import datetime

logger = logging.getLogger(__name__)

FIRST_COLLECTION_DELAY_SECONDS = 2.0
COLLECTION_BACKOFF_BASE_SECONDS = 2
COLLECTION_BACKOFF_MAX_SECONDS = 60
# Longer than the longest backoff, so a job whose task chain is alive is
# never swept.
COLLECTION_STALE_AFTER = timedelta(minutes=5)
DISPATCH_TIMEOUT = timedelta(minutes=10)
SWEEP_BATCH_SIZE = 200
SWEEP_CONCURRENCY = 10
MODAL_CALL_ID_PATTERN = re.compile(r"fc-[0-9A-Za-z]+")
CLOUD_TASKS_API = "https://cloudtasks.googleapis.com/v2"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
CLOUD_TASKS_TIMEOUT_SECONDS = 5.0


def collection_delay_seconds(attempts: int) -> int:
    """Return the wait before the next check after ``attempts`` checks."""
    return min(
        COLLECTION_BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0),
        COLLECTION_BACKOFF_MAX_SECONDS,
    )


@dataclass(frozen=True)
class JobKind:
    """A kind of calculation the job store can collect.

    A report kind (``report_kind`` set) stores its result in
    ``simulation.report_results``, validated by the aggregate model registered
    for that report kind and ``result_schema_version``; any other kind stores
    it in ``simulation.job_results``. Step 2 registers the household
    calculation and report kinds; until then no kind is registered and nothing
    submits jobs.
    """

    name: str
    result_model: type[BaseModel]
    result_schema_version: int
    report_kind: ReportKind | None = None


# The job kinds production collects, by name.
REGISTERED_JOB_KINDS: dict[str, JobKind] = {}


@dataclass(frozen=True)
class ModalTarget:
    """The deployed Modal function a job calls."""

    application: str
    function: str


@dataclass(frozen=True)
class CallRunning:
    """The Modal call has not produced its output yet."""


@dataclass(frozen=True)
class CallSucceeded:
    """The Modal call returned ``output``."""

    output: object


@dataclass(frozen=True)
class CallFailed:
    """The Modal call failed; the message carries no worker data."""

    error_code: str
    error_message: str


type CallOutcome = CallRunning | CallSucceeded | CallFailed


class CallInspectionError(RuntimeError):
    """Modal could not be asked about a call; try again later."""


class CollectionQueueError(RuntimeError):
    """Cloud Tasks did not accept a collection task."""


class UnknownJobKindError(ValueError):
    """The job kind is not registered."""


class CallDispatcher(Protocol):
    """Start a Modal call without waiting for it."""

    async def spawn(
        self,
        target: ModalTarget,
        request_payload: dict[str, Any],
        *,
        observability_context: dict[str, Any] | None,
    ) -> str:
        """Return the acknowledged Modal call ID."""
        ...


class CallInspector(Protocol):
    """Check a Modal call without waiting for it."""

    async def inspect(self, modal_call_id: str) -> CallOutcome:
        """Return the call's outcome; raise CallInspectionError if unknown."""
        ...


class CollectionQueue(Protocol):
    """Schedule a collection task for a job."""

    async def enqueue(self, job_id: UUID, *, delay_seconds: float) -> None:
        """Schedule one collection; raise CollectionQueueError if refused."""
        ...


class JobStoreOperations(Protocol):
    """The ``JobStore`` operations the service uses."""

    def submit(self, new_job: NewJob) -> Submission:
        """Create the job, or return the existing one for the same input."""
        ...

    def claim_dispatch(self, job_id: UUID) -> bool:
        """Move a pending job to dispatching."""
        ...

    def record_dispatched(self, job_id: UUID, *, modal_call_id: str) -> bool:
        """Move a dispatching job to running."""
        ...

    def record_dispatch_failure(
        self, job_id: UUID, *, error_code: str, error_message: str
    ) -> bool:
        """Fail a dispatching job."""
        ...

    def get(self, job_id: UUID) -> JobRecord | None:
        """Return a job by ID."""
        ...

    def get_by_modal_call_id(self, modal_call_id: str) -> JobRecord | None:
        """Return a job by Modal call ID."""
        ...

    def get_result(self, job_id: UUID) -> JobResultRecord | None:
        """Return a succeeded job's result."""
        ...

    def get_report_result(self, job_id: UUID) -> ReportResultRecord | None:
        """Return a succeeded report job's result."""
        ...

    def record_check(self, job_id: UUID) -> int | None:
        """Count one check of a running job."""
        ...

    def complete(
        self, job_id: UUID, *, result: dict[str, Any], result_schema_version: int
    ) -> bool:
        """Store a running job's result."""
        ...

    def complete_report(
        self,
        job_id: UUID,
        *,
        report_kind: ReportKind,
        aggregate: object,
        aggregate_schema_version: int,
    ) -> bool:
        """Store a running report job's validated aggregate."""
        ...

    def fail(self, job_id: UUID, *, error_code: str, error_message: str) -> bool:
        """Fail a running job."""
        ...

    def jobs_to_collect(self, *, checked_before: datetime, limit: int) -> list[UUID]:
        """Return running jobs not checked recently."""
        ...

    def abandon_stalled_dispatches(self, *, updated_before: datetime) -> list[UUID]:
        """Fail jobs whose dispatch stalled."""
        ...


class CollectionOutcome(StrEnum):
    """What one collection task did."""

    UNKNOWN_JOB = "unknown_job"
    NOT_RUNNING = "not_running"
    STILL_RUNNING = "still_running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ALREADY_FINISHED = "already_finished"


@dataclass(frozen=True)
class CollectionReport:
    """The result of one collection task."""

    outcome: CollectionOutcome
    job_status: JobStatus | None
    next_check_seconds: float | None = None


@dataclass(frozen=True)
class SweepReport:
    """What one sweep did."""

    abandoned: int
    enqueued: int
    enqueue_failures: int


@dataclass(frozen=True)
class JobView:
    """A job and, once it succeeded, its stored result."""

    job: JobRecord
    result: JobResultRecord | ReportResultRecord | None


@dataclass
class SimulationJobService:
    """Submit simulation jobs and collect their results in the background."""

    store: JobStoreOperations
    dispatcher: CallDispatcher
    inspector: CallInspector
    queue: CollectionQueue
    job_kinds: Mapping[str, JobKind] = field(default_factory=dict)
    clock: Callable[[], datetime] = utc_now

    # --- Submission ------------------------------------------------------------

    async def submit(  # noqa: PLR0913  # every argument is part of the job's identity or provenance
        self,
        *,
        kind: str,
        request_payload: dict[str, Any],
        policyengine_version: str,
        submitted_by: str,
        target: ModalTarget,
        observability_id: str | None = None,
        observability_context: dict[str, Any] | None = None,
    ) -> JobRecord:
        """Record the job and start its Modal call unless one already exists.

        Return the job as it stands afterwards. A refused Modal call fails the
        job instead of raising; a refused collection task is left to the sweep.
        """
        if kind not in self.job_kinds:
            raise UnknownJobKindError(kind)
        submission = await asyncio.to_thread(
            self.store.submit,
            NewJob(
                kind=kind,
                submitted_by=submitted_by,
                input_sha256=input_sha256(
                    kind=kind,
                    policyengine_version=policyengine_version,
                    request=request_payload,
                ),
                policyengine_version=policyengine_version,
                modal_application=target.application,
                modal_function=target.function,
                observability_id=observability_id,
            ),
        )
        job = submission.job
        if not submission.created or not await asyncio.to_thread(
            self.store.claim_dispatch, job.id
        ):
            return await self._current(job)
        try:
            modal_call_id = await self.dispatcher.spawn(
                target,
                request_payload,
                observability_context=observability_context,
            )
        # Any failure to start the call fails the job; the caller may resubmit.
        except Exception as error:  # noqa: BLE001  # recorded on the job
            logger.warning(
                "simulation_job_dispatch_failed",
                extra={"job_id": str(job.id), "error_type": type(error).__name__},
            )
            await asyncio.to_thread(
                self.store.record_dispatch_failure,
                job.id,
                error_code="dispatch_failed",
                error_message=(
                    f"Modal did not accept the call ({type(error).__name__})."
                ),
            )
            return await self._current(job)
        await asyncio.to_thread(
            self.store.record_dispatched, job.id, modal_call_id=modal_call_id
        )
        try:
            await self.queue.enqueue(
                job.id, delay_seconds=FIRST_COLLECTION_DELAY_SECONDS
            )
        except CollectionQueueError as error:
            logger.warning(
                "simulation_job_collection_not_scheduled",
                extra={"job_id": str(job.id), "error_type": type(error).__name__},
            )
        return await self._current(job)

    async def _current(self, job: JobRecord) -> JobRecord:
        return await asyncio.to_thread(self.store.get, job.id) or job

    # --- Collection --------------------------------------------------------------

    async def collect(self, job_id: UUID) -> CollectionReport:
        """Check one job's Modal call and record what it found.

        Raise CallInspectionError or CollectionQueueError for a transient
        failure, so Cloud Tasks retries this task.
        """
        job = await asyncio.to_thread(self.store.get, job_id)
        if job is None:
            return CollectionReport(CollectionOutcome.UNKNOWN_JOB, None)
        if job.status is not JobStatus.RUNNING or job.modal_call_id is None:
            return CollectionReport(CollectionOutcome.NOT_RUNNING, job.status)
        outcome = await self.inspector.inspect(job.modal_call_id)
        if isinstance(outcome, CallRunning):
            attempts = await asyncio.to_thread(self.store.record_check, job.id)
            if attempts is None:
                return await self._finished_elsewhere(job.id)
            delay = collection_delay_seconds(attempts)
            await self.queue.enqueue(job.id, delay_seconds=delay)
            return CollectionReport(
                CollectionOutcome.STILL_RUNNING, JobStatus.RUNNING, delay
            )
        if isinstance(outcome, CallFailed):
            return await self._fail(job.id, outcome.error_code, outcome.error_message)
        return await self._complete(job, outcome.output)

    async def _complete(self, job: JobRecord, output: object) -> CollectionReport:
        kind = self.job_kinds.get(job.kind)
        if kind is None:
            return await self._fail(
                job.id,
                "unknown_job_kind",
                f"No collector is registered for job kind {job.kind!r}.",
            )
        if kind.report_kind is not None:
            return await self._complete_report(job, kind, output)
        try:
            result = kind.result_model.model_validate(output).model_dump(mode="json")
        except ValidationError as error:
            return await self._fail(
                job.id,
                "invalid_result",
                f"The result failed validation with {error.error_count()} errors.",
            )
        if not isinstance(result, dict):
            return await self._fail(
                job.id, "invalid_result", "The result is not a JSON object."
            )
        stored = await asyncio.to_thread(
            self.store.complete,
            job.id,
            result=result,
            result_schema_version=kind.result_schema_version,
        )
        if not stored:
            return await self._finished_elsewhere(job.id)
        return CollectionReport(CollectionOutcome.SUCCEEDED, JobStatus.SUCCEEDED)

    async def _complete_report(
        self, job: JobRecord, kind: JobKind, output: object
    ) -> CollectionReport:
        try:
            stored = await asyncio.to_thread(
                lambda: self.store.complete_report(
                    job.id,
                    report_kind=kind.report_kind,
                    aggregate=output,
                    aggregate_schema_version=kind.result_schema_version,
                )
            )
        except ValidationError as error:
            return await self._fail(
                job.id,
                "invalid_result",
                f"The aggregate failed validation with {error.error_count()} errors.",
            )
        except UnsupportedReportAggregateError:
            return await self._fail(
                job.id,
                "invalid_result",
                "No aggregate model is registered for this report.",
            )
        if not stored:
            return await self._finished_elsewhere(job.id)
        return CollectionReport(CollectionOutcome.SUCCEEDED, JobStatus.SUCCEEDED)

    async def _fail(
        self, job_id: UUID, error_code: str, error_message: str
    ) -> CollectionReport:
        failed = await asyncio.to_thread(
            self.store.fail,
            job_id,
            error_code=error_code,
            error_message=error_message,
        )
        if not failed:
            return await self._finished_elsewhere(job_id)
        return CollectionReport(CollectionOutcome.FAILED, JobStatus.FAILED)

    async def _finished_elsewhere(self, job_id: UUID) -> CollectionReport:
        job = await asyncio.to_thread(self.store.get, job_id)
        return CollectionReport(
            CollectionOutcome.ALREADY_FINISHED,
            None if job is None else job.status,
        )

    # --- The sweep ------------------------------------------------------------------

    async def sweep(self) -> SweepReport:
        """Fail stalled dispatches and enqueue running jobs not checked recently."""
        now = self.clock()
        abandoned = await asyncio.to_thread(
            lambda: self.store.abandon_stalled_dispatches(
                updated_before=now - DISPATCH_TIMEOUT
            )
        )
        job_ids = await asyncio.to_thread(
            lambda: self.store.jobs_to_collect(
                checked_before=now - COLLECTION_STALE_AFTER,
                limit=SWEEP_BATCH_SIZE,
            )
        )
        limit = asyncio.Semaphore(SWEEP_CONCURRENCY)

        async def enqueue(job_id: UUID) -> bool:
            async with limit:
                try:
                    await self.queue.enqueue(job_id, delay_seconds=0)
                except CollectionQueueError as error:
                    logger.warning(
                        "simulation_job_sweep_enqueue_failed",
                        extra={
                            "job_id": str(job_id),
                            "error_type": type(error).__name__,
                        },
                    )
                    return False
                return True

        enqueued = await asyncio.gather(*(enqueue(job_id) for job_id in job_ids))
        return SweepReport(
            abandoned=len(abandoned),
            enqueued=enqueued.count(True),
            enqueue_failures=enqueued.count(False),
        )

    # --- Status reads ----------------------------------------------------------------

    async def find(self, reference: str) -> JobView | None:
        """Return the job a UUID or Modal ``fc-`` call ID names, from the database."""
        try:
            job_id = UUID(reference)
        except ValueError:
            if not MODAL_CALL_ID_PATTERN.fullmatch(reference):
                return None
            job = await asyncio.to_thread(self.store.get_by_modal_call_id, reference)
        else:
            job = await asyncio.to_thread(self.store.get, job_id)
        if job is None:
            return None
        result: JobResultRecord | ReportResultRecord | None = None
        if job.status is JobStatus.SUCCEEDED:
            kind = self.job_kinds.get(job.kind)
            reads_report = kind is not None and kind.report_kind is not None
            result = await asyncio.to_thread(
                self.store.get_report_result if reads_report else self.store.get_result,
                job.id,
            )
        return JobView(job=job, result=result)


# --- Modal ------------------------------------------------------------------------

# Modal client and service problems: the call's state is unknown, so retry.
TRANSIENT_MODAL_ERRORS = (
    modal.exception.ConnectionError,
    modal.exception.AuthError,
    modal.exception.InternalError,
    modal.exception.ServiceError,
    modal.exception.ResourceExhaustedError,
)


class ModalCallDispatcher:
    """Spawn deployed Modal functions in one Modal environment."""

    def __init__(self, environment: str) -> None:
        """Look functions up in ``environment``."""
        self._environment = environment

    async def spawn(
        self,
        target: ModalTarget,
        request_payload: dict[str, Any],
        *,
        observability_context: dict[str, Any] | None,
    ) -> str:
        """Spawn ``target`` and return Modal's call ID once Modal acknowledges it."""
        function = modal.Function.from_name(
            target.application,
            target.function,
            environment_name=self._environment,
        )
        call = await function.spawn.aio(
            request_payload,
            observability_context=observability_context,
        )
        modal_call_id = getattr(call, "object_id", None)
        if not isinstance(modal_call_id, str) or not modal_call_id:
            msg = "Modal acknowledged the spawn with no call ID"
            raise RuntimeError(msg)
        return modal_call_id


class ModalCallInspector:
    """Read a Modal call's output without waiting for it."""

    async def inspect(self, modal_call_id: str) -> CallOutcome:  # noqa: PLR0911  # one return per Modal outcome
        """Return the call's outcome, or raise CallInspectionError."""
        try:
            call = modal.FunctionCall.from_id(modal_call_id)
            output = await call.get.aio(timeout=0)
        except modal.exception.OutputExpiredError:
            return CallFailed("result_expired", "Modal no longer holds the output.")
        except modal.exception.FunctionTimeoutError:
            return CallFailed("worker_timeout", "The Modal call timed out.")
        except modal.exception.NotFoundError:
            return CallFailed("call_not_found", "Modal does not know the call.")
        except TRANSIENT_MODAL_ERRORS as error:
            raise CallInspectionError(type(error).__name__) from error
        except TimeoutError:
            return CallRunning()
        # The call re-raises whatever the worker raised. Record only its type:
        # the message may contain calculation inputs.
        except Exception as error:  # noqa: BLE001  # every worker failure fails the job
            return CallFailed(
                "worker_error",
                f"The Modal call raised {type(error).__name__}.",
            )
        return CallSucceeded(output)


# --- Cloud Tasks -------------------------------------------------------------------


class GoogleAccessTokens:
    """OAuth access tokens from the runtime's default Google credentials."""

    def __init__(self) -> None:
        """Load credentials on first use."""
        self._credentials: Any = None
        self._lock = threading.Lock()

    def _token(self) -> str:
        with self._lock:
            if self._credentials is None:
                self._credentials, _ = google.auth.default(
                    scopes=[CLOUD_PLATFORM_SCOPE]
                )
            if not self._credentials.valid:
                self._credentials.refresh(google.auth.transport.requests.Request())
            return self._credentials.token

    async def __call__(self) -> str:
        """Return a valid access token, refreshing it off the event loop."""
        return await asyncio.to_thread(self._token)


class CloudTasksCollectionQueue:
    """Create Cloud Tasks HTTP tasks that call the collection route."""

    def __init__(  # noqa: PLR0913  # the queue, target, identity and three test seams
        self,
        *,
        queue: str,
        target_url: str,
        invoker_service_account: str,
        access_token: Callable[[], Awaitable[str]] | None = None,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Send tasks to ``queue`` that call ``target_url`` as the invoker."""
        self.queue = queue
        self.target_url = target_url.rstrip("/")
        self.invoker_service_account = invoker_service_account
        self._access_token = access_token or GoogleAccessTokens()
        self._http_client = http_client or httpx.AsyncClient(
            timeout=CLOUD_TASKS_TIMEOUT_SECONDS
        )
        self._clock = clock

    def collection_url(self, job_id: UUID) -> str:
        """Return the collection route URL for ``job_id``."""
        return f"{self.target_url}/internal/simulation-jobs/{job_id}/collect"

    async def enqueue(self, job_id: UUID, *, delay_seconds: float) -> None:
        """Create one task that calls the collection route after the delay."""
        task: dict[str, Any] = {
            "httpRequest": {
                "httpMethod": "POST",
                "url": self.collection_url(job_id),
                "oidcToken": {
                    "serviceAccountEmail": self.invoker_service_account,
                    "audience": self.target_url,
                },
            }
        }
        if delay_seconds > 0:
            scheduled = self._clock() + timedelta(seconds=delay_seconds)
            task["scheduleTime"] = scheduled.isoformat().replace("+00:00", "Z")
        try:
            token = await self._access_token()
            response = await self._http_client.post(
                f"{CLOUD_TASKS_API}/{self.queue}/tasks",
                json={"task": task},
                headers={"Authorization": f"Bearer {token}"},
            )
        except (httpx.HTTPError, google.auth.exceptions.GoogleAuthError) as error:
            msg = f"Cloud Tasks could not be reached ({type(error).__name__})"
            raise CollectionQueueError(msg) from error
        if response.status_code >= httpx.codes.MULTIPLE_CHOICES:
            msg = f"Cloud Tasks refused the task with HTTP {response.status_code}"
            raise CollectionQueueError(msg)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http_client.aclose()
