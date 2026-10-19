"""The simulation job store: one ``simulation.jobs`` row per calculation.

The Simulation Entrypoint writes every row; Modal never does. A job moves
``pending`` -> ``dispatching`` -> ``running`` -> ``succeeded`` or ``failed``.
Every transition is one conditional ``UPDATE ... WHERE status = <expected>``,
so when two entrypoint instances act on the same job at once, PostgreSQL makes
the second wait for the first's row lock and then find nothing to change. No
lease or lock table is needed:

- submission is idempotent: identical inputs share one unfinished or
  succeeded job (``INSERT ... ON CONFLICT DO NOTHING`` on the input hash);
- only the instance that moves a job from ``pending`` to ``dispatching``
  starts its Modal call;
- only one collector moves a ``running`` job to ``succeeded`` (writing its
  result in the same transaction) or ``failed``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    Uuid,
    func,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from policyengine_simulation_persistence.schema import simulation_metadata

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Connection, Engine, RowMapping

MAX_ERROR_MESSAGE_LENGTH = 2000
DISPATCH_ABANDONED = "dispatch_abandoned"


class JobStatus(StrEnum):
    """Where a job is in its lifecycle."""

    PENDING = "pending"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


UNFINISHED_STATUSES = (JobStatus.PENDING, JobStatus.DISPATCHING, JobStatus.RUNNING)
_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in JobStatus)
_SHA256_PATTERN = "^[0-9a-f]{64}$"

jobs = Table(
    "jobs",
    simulation_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("submitted_by", Text, nullable=False),
    Column("input_sha256", String(64), nullable=False),
    Column("policyengine_version", Text, nullable=False),
    Column("modal_application", Text, nullable=False),
    Column("modal_function", Text, nullable=False),
    Column("modal_call_id", Text, nullable=True, unique=True),
    Column("observability_id", Text, nullable=True),
    Column("error_code", Text, nullable=True),
    Column("error_message", Text, nullable=True),
    Column("collection_attempts", Integer, nullable=False, server_default=text("0")),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("dispatched_at", DateTime(timezone=True), nullable=True),
    Column("last_checked_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status"),
    CheckConstraint(f"input_sha256 ~ '{_SHA256_PATTERN}'", name="input_sha256"),
    CheckConstraint("collection_attempts >= 0", name="collection_attempts"),
)
# Identical inputs share one job unless it failed; a failed job never blocks a
# new submission of the same input.
Index(
    "uq_jobs_input_sha256_unfailed",
    jobs.c.input_sha256,
    unique=True,
    postgresql_where=jobs.c.status != JobStatus.FAILED.value,
)
# The scheduled sweep reads only unfinished jobs.
Index(
    "ix_jobs_unfinished_status",
    jobs.c.status,
    postgresql_where=jobs.c.status.in_(
        [status.value for status in UNFINISHED_STATUSES]
    ),
)

job_results = Table(
    "job_results",
    simulation_metadata,
    Column(
        "job_id",
        Uuid(as_uuid=True),
        ForeignKey(jobs.c.id),
        primary_key=True,
    ),
    Column("result", JSONB, nullable=False),
    Column("result_schema_version", Integer, nullable=False),
    Column("result_sha256", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(f"result_sha256 ~ '{_SHA256_PATTERN}'", name="result_sha256"),
    CheckConstraint("result_schema_version >= 1", name="result_schema_version"),
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def input_sha256(*, kind: str, policyengine_version: str, request: object) -> str:
    """Return the idempotency hash of one calculation's inputs.

    It covers the job kind, the policyengine.py bundle version and the request
    as canonical JSON, so the same request under another bundle is another job.
    """
    return hashlib.sha256(
        _canonical_json(
            {
                "kind": kind,
                "policyengine_version": policyengine_version,
                "request": request,
            }
        )
    ).hexdigest()


def result_sha256(result: object) -> str:
    """Return the SHA-256 of a result as canonical JSON."""
    return hashlib.sha256(_canonical_json(result)).hexdigest()


def utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class NewJob:
    """What a submission records before anything is dispatched."""

    kind: str
    submitted_by: str
    input_sha256: str
    policyengine_version: str
    modal_application: str
    modal_function: str
    observability_id: str | None = None


@dataclass(frozen=True)
class JobRecord:
    """One ``simulation.jobs`` row."""

    id: UUID
    kind: str
    status: JobStatus
    submitted_by: str
    input_sha256: str
    policyengine_version: str
    modal_application: str
    modal_function: str
    modal_call_id: str | None
    observability_id: str | None
    error_code: str | None
    error_message: str | None
    collection_attempts: int
    created_at: datetime
    updated_at: datetime
    dispatched_at: datetime | None
    last_checked_at: datetime | None
    completed_at: datetime | None

    @property
    def finished(self) -> bool:
        """Whether the job has reached ``succeeded`` or ``failed``."""
        return self.status not in UNFINISHED_STATUSES

    @classmethod
    def from_row(cls, row: RowMapping) -> JobRecord:
        """Build the record from a selected ``jobs`` row."""
        values = {field.name: row[field.name] for field in fields(cls)}
        values["status"] = JobStatus(values["status"])
        return cls(**values)


@dataclass(frozen=True)
class JobResultRecord:
    """One ``simulation.job_results`` row."""

    job_id: UUID
    result: dict[str, Any]
    result_schema_version: int
    result_sha256: str
    created_at: datetime


@dataclass(frozen=True)
class Submission:
    """The job a submission resolved to, and whether this call created it."""

    job: JobRecord
    created: bool


def _bounded(message: str) -> str:
    return message[:MAX_ERROR_MESSAGE_LENGTH]


class JobStore:
    """Typed SQLAlchemy Core reads and conditional writes for simulation jobs."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Use ``engine`` (the runtime role) and ``clock`` for every timestamp."""
        self.engine = engine
        self._clock = clock

    # --- Submission and dispatch ---------------------------------------------

    def submit(self, new_job: NewJob) -> Submission:
        """Create a pending job, or return the existing one for the same input.

        A failed job does not count: the same input then creates a new job.
        """
        now = self._clock()
        values = {
            "id": uuid4(),
            "status": JobStatus.PENDING.value,
            "created_at": now,
            "updated_at": now,
            **{field.name: getattr(new_job, field.name) for field in fields(new_job)},
        }
        statement = (
            postgres_insert(jobs)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[jobs.c.input_sha256],
                index_where=jobs.c.status != JobStatus.FAILED.value,
            )
            .returning(*jobs.c)
        )
        existing_statement = select(jobs).where(
            jobs.c.input_sha256 == new_job.input_sha256,
            jobs.c.status != JobStatus.FAILED.value,
        )
        # The conflicting job can fail between the insert and the read; the
        # next attempt then creates a new job.
        for _attempt in range(3):
            with self.engine.begin() as connection:
                created = connection.execute(statement).mappings().first()
                if created is not None:
                    return Submission(job=JobRecord.from_row(created), created=True)
                existing = connection.execute(existing_statement).mappings().first()
                if existing is not None:
                    return Submission(job=JobRecord.from_row(existing), created=False)
        msg = "job submission did not settle on one job"
        raise RuntimeError(msg)

    def _claim_dispatch(self, connection: Connection, job_id: UUID) -> bool:
        claimed = connection.execute(
            update(jobs)
            .where(jobs.c.id == job_id, jobs.c.status == JobStatus.PENDING.value)
            .values(status=JobStatus.DISPATCHING.value, updated_at=self._clock())
            .returning(jobs.c.id)
        ).first()
        return claimed is not None

    def claim_dispatch(self, job_id: UUID) -> bool:
        """Move a pending job to dispatching; only the winner starts its call."""
        with self.engine.begin() as connection:
            return self._claim_dispatch(connection, job_id)

    def record_dispatched(self, job_id: UUID, *, modal_call_id: str) -> bool:
        """Record the acknowledged Modal call and move the job to running."""
        now = self._clock()
        with self.engine.begin() as connection:
            return (
                connection.execute(
                    update(jobs)
                    .where(
                        jobs.c.id == job_id,
                        jobs.c.status == JobStatus.DISPATCHING.value,
                    )
                    .values(
                        status=JobStatus.RUNNING.value,
                        modal_call_id=modal_call_id,
                        dispatched_at=now,
                        updated_at=now,
                    )
                    .returning(jobs.c.id)
                ).first()
                is not None
            )

    def record_dispatch_failure(
        self,
        job_id: UUID,
        *,
        error_code: str,
        error_message: str,
    ) -> bool:
        """Fail a job whose Modal call was refused or never acknowledged."""
        with self.engine.begin() as connection:
            return self._finish_failed(
                connection,
                job_id,
                expected=JobStatus.DISPATCHING,
                error_code=error_code,
                error_message=error_message,
            )

    # --- Reads -----------------------------------------------------------------

    def get(self, job_id: UUID) -> JobRecord | None:
        """Return the job with this ID, or None."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(select(jobs).where(jobs.c.id == job_id))
                .mappings()
                .first()
            )
        return None if row is None else JobRecord.from_row(row)

    def get_by_modal_call_id(self, modal_call_id: str) -> JobRecord | None:
        """Return the job whose Modal call has this ID, or None."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(jobs).where(jobs.c.modal_call_id == modal_call_id)
                )
                .mappings()
                .first()
            )
        return None if row is None else JobRecord.from_row(row)

    def get_result(self, job_id: UUID) -> JobResultRecord | None:
        """Return the stored result of a succeeded job, or None."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(job_results).where(job_results.c.job_id == job_id)
                )
                .mappings()
                .first()
            )
        return None if row is None else JobResultRecord(**row)

    # --- Collection ------------------------------------------------------------

    def record_check(self, job_id: UUID) -> int | None:
        """Count one check of a still-running call; None if it is not running."""
        now = self._clock()
        with self.engine.begin() as connection:
            return connection.execute(
                update(jobs)
                .where(jobs.c.id == job_id, jobs.c.status == JobStatus.RUNNING.value)
                .values(
                    collection_attempts=jobs.c.collection_attempts + 1,
                    last_checked_at=now,
                    updated_at=now,
                )
                .returning(jobs.c.collection_attempts)
            ).scalar()

    def _complete(
        self,
        connection: Connection,
        job_id: UUID,
        *,
        result: dict[str, Any],
        result_schema_version: int,
    ) -> bool:
        now = self._clock()
        finished = connection.execute(
            update(jobs)
            .where(jobs.c.id == job_id, jobs.c.status == JobStatus.RUNNING.value)
            .values(
                status=JobStatus.SUCCEEDED.value,
                completed_at=now,
                last_checked_at=now,
                updated_at=now,
            )
            .returning(jobs.c.id)
        ).first()
        if finished is None:
            return False
        connection.execute(
            insert(job_results).values(
                job_id=job_id,
                result=result,
                result_schema_version=result_schema_version,
                result_sha256=result_sha256(result),
                created_at=now,
            )
        )
        return True

    def complete(
        self,
        job_id: UUID,
        *,
        result: dict[str, Any],
        result_schema_version: int,
    ) -> bool:
        """Store a running job's validated result and mark it succeeded.

        Return False, writing nothing, when the job is no longer running
        because another collector already finished it.
        """
        with self.engine.begin() as connection:
            return self._complete(
                connection,
                job_id,
                result=result,
                result_schema_version=result_schema_version,
            )

    def _finish_failed(
        self,
        connection: Connection,
        job_id: UUID,
        *,
        expected: JobStatus,
        error_code: str,
        error_message: str,
    ) -> bool:
        now = self._clock()
        return (
            connection.execute(
                update(jobs)
                .where(jobs.c.id == job_id, jobs.c.status == expected.value)
                .values(
                    status=JobStatus.FAILED.value,
                    error_code=error_code,
                    error_message=_bounded(error_message),
                    completed_at=now,
                    updated_at=now,
                )
                .returning(jobs.c.id)
            ).first()
            is not None
        )

    def _fail(
        self,
        connection: Connection,
        job_id: UUID,
        *,
        error_code: str,
        error_message: str,
    ) -> bool:
        return self._finish_failed(
            connection,
            job_id,
            expected=JobStatus.RUNNING,
            error_code=error_code,
            error_message=error_message,
        )

    def fail(self, job_id: UUID, *, error_code: str, error_message: str) -> bool:
        """Mark a running job failed; False if another collector finished it."""
        with self.engine.begin() as connection:
            return self._fail(
                connection,
                job_id,
                error_code=error_code,
                error_message=error_message,
            )

    # --- The scheduled sweep ---------------------------------------------------

    def jobs_to_collect(self, *, checked_before: datetime, limit: int) -> list[UUID]:
        """Return running jobs nobody has checked since ``checked_before``."""
        last_activity = func.coalesce(
            jobs.c.last_checked_at,
            jobs.c.dispatched_at,
            jobs.c.updated_at,
        )
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    select(jobs.c.id)
                    .where(
                        jobs.c.status == JobStatus.RUNNING.value,
                        last_activity < checked_before,
                    )
                    .order_by(last_activity)
                    .limit(limit)
                ).scalars()
            )

    def abandon_stalled_dispatches(self, *, updated_before: datetime) -> list[UUID]:
        """Fail jobs stuck before their Modal call was recorded.

        A job stays ``pending`` or ``dispatching`` only if the instance
        submitting it stopped before recording the call. Failing it lets the
        same input be submitted again.
        """
        now = self._clock()
        with self.engine.begin() as connection:
            return list(
                connection.execute(
                    update(jobs)
                    .where(
                        or_(
                            jobs.c.status == JobStatus.PENDING.value,
                            jobs.c.status == JobStatus.DISPATCHING.value,
                        ),
                        jobs.c.updated_at < updated_before,
                    )
                    .values(
                        status=JobStatus.FAILED.value,
                        error_code=DISPATCH_ABANDONED,
                        error_message=(
                            "The submitting instance stopped before the Modal "
                            "call was recorded."
                        ),
                        completed_at=now,
                        updated_at=now,
                    )
                    .returning(jobs.c.id)
                ).scalars()
            )
