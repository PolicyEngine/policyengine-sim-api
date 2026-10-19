"""Simulation Entry schemas that are not part of the public gateway contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypedDict
from uuid import UUID

from policyengine_simulation_contract.json_types import JsonObject
from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator


class CallerIdentity(BaseModel):
    """Validated identity claims from an inbound Auth0 access token.

    Aliased fields are read only by their registered JWT claim names, never by
    field name: a token's ``grant_types`` or ``subject`` claim must not stand
    in for ``gty`` or ``sub``, because those decide whether ``scope`` counts.
    """

    subject: str | None = Field(default=None, alias="sub")
    issuer: str = Field(alias="iss")
    audience: str | list[str] = Field(alias="aud")
    expires_at: int = Field(alias="exp")
    issued_at: int | None = Field(default=None, alias="iat")
    scope: str | None = None
    permissions: list[str] | None = None
    authorized_party: str | None = Field(default=None, alias="azp")
    grant_types: tuple[str, ...] = Field(default=(), alias="gty")

    model_config = ConfigDict(extra="ignore")

    @field_validator("grant_types", mode="before")
    @classmethod
    def _grant_types_from_claim(cls, claim: object) -> tuple[str, ...]:
        """Read ``gty`` as a string or a list of strings.

        Auth0 may issue it as a list (for example a refresh-token grant). Any
        other shape is ignored rather than rejecting an otherwise valid token,
        because the claim only widens what ``scope`` may grant.
        """
        if isinstance(claim, str):
            return (claim,)
        if isinstance(claim, list):
            return tuple(item for item in claim if isinstance(item, str))
        return ()


class BackendServiceToken(BaseModel):
    """Auth0 client-credentials response used for old-gateway calls."""

    access_token: str = Field(min_length=1)
    expires_in: int = Field(ge=1)


type BackendJobState = Literal[
    "submitted",
    "pending",
    "queued",
    "running",
    "complete",
    "failed",
    "cancelled",
]


class BackendTelemetryPayload(BaseModel):
    """Response fields that are safe and useful for proxy telemetry."""

    status: BackendJobState | None = None
    job_id: str | None = None
    batch_job_id: str | None = None


class RequestIdentifiers(TypedDict, total=False):
    """Identifiers captured from a templated request route."""

    job_id: str
    batch_job_id: str
    evaluation_id: str


class BackendTelemetryAttributes(TypedDict, total=False):
    """Structured fields emitted for one old-gateway response."""

    request_id: str
    route: str
    method: str
    status_code: int
    elapsed_ms: float
    job_id: str
    batch_job_id: str
    job_state: BackendJobState


# TEMPORARY(Stage 12): Remove these operator-only HTTP models when Stage 14
# replaces the comparison tables with authoritative report submission and polling.
class TemporaryStage12SubmissionResponse(BaseModel):
    """Acknowledgement for one temporary direct Stage 12 report submission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_id: UUID
    status: ComparisonRunLifecycleStatus
    poll_url: str


class TemporaryStage12ReportResponse(BaseModel):
    """Temporary parent and child execution metadata; never report contents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report: ComparisonReportRecord
    simulations: tuple[ComparisonSimulationRecord, ...]


class SimulationDatabaseReadiness(BaseModel):
    """The Simulation Entrypoint's own database, as the readiness check found it.

    ``revision`` is the migration revision the database has applied, or null
    when it was unreachable or has no migrations yet; ``expected_revision`` is
    the newest revision this image knows. Neither changes ``status``: no
    old-gateway or Stage 12 route uses this database.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reachable: bool
    revision: str | None
    expected_revision: str


class SimulationEntrypointReadinessResponse(BaseModel):
    """Old-gateway readiness, plus the database revision when one is configured."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready", "not_ready"]
    simulation_database: SimulationDatabaseReadiness | None = None


class SimulationJobError(BaseModel):
    """Why a simulation job failed; never worker data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str | None


class SimulationJobStatusResponse(BaseModel):
    """One simulation job as the database records it.

    ``result`` is present once the job succeeded. Reading a job never checks
    or advances its Modal call.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    kind: str
    status: Literal["pending", "dispatching", "running", "succeeded", "failed"]
    policyengine_version: str
    modal_call_id: str | None
    created_at: datetime
    dispatched_at: datetime | None
    completed_at: datetime | None
    error: SimulationJobError | None
    result: JsonObject | None
    result_sha256: str | None


class SimulationJobCollectionResponse(BaseModel):
    """What one Cloud Tasks collection request did."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal[
        "unknown_job",
        "not_running",
        "still_running",
        "succeeded",
        "failed",
        "already_finished",
    ]
    job_status: (
        Literal["pending", "dispatching", "running", "succeeded", "failed"] | None
    )
    next_check_seconds: float | None


class SimulationJobSweepResponse(BaseModel):
    """What one Cloud Scheduler sweep did."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    abandoned: int
    enqueued: int
    enqueue_failures: int
