"""Simulation Entry schemas that are not part of the public gateway contract."""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class CallerIdentity(BaseModel):
    """Validated identity claims from an inbound Auth0 access token."""

    subject: str | None = Field(default=None, alias="sub")
    issuer: str = Field(alias="iss")
    audience: str | list[str] = Field(alias="aud")
    expires_at: int = Field(alias="exp")
    issued_at: int | None = Field(default=None, alias="iat")
    scope: str | None = None
    permissions: list[str] | None = None
    authorized_party: str | None = Field(default=None, alias="azp")

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


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
