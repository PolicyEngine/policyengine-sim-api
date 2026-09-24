"""Allowlisted correlation metadata for asynchronous simulation dispatch."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from policyengine_simulation_observability.identifiers import (
    normalize_observability_id,
)

CaptureMode = Literal["disabled", "failures", "threshold", "sampled", "always"]
NONCANONICAL_CONTEXT_FIELDS = frozenset(
    {"observability_id", "run_id", "request_id", "traceparent"}
)


class ObservabilityContext(BaseModel):
    traceparent: str | None = None
    tracestate: str | None = None
    captured_at: datetime
    request_id: str | None = None
    job_id: str | None = None
    observability_id: str | None = None
    simulation_id: str | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("observability_id")
    @classmethod
    def validate_observability_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_observability_id(value)
        if normalized is None:
            raise ValueError("observability_id must be a UUID")
        return normalized


class TelemetryEnvelope(BaseModel):
    """Bounded internal metadata passed from API request to Modal worker."""

    submission_claim_id: str | None = None
    requested_at: datetime | None = None
    simulation_kind: str | None = None
    geography_code: str | None = None
    geography_type: str | None = None
    config_hash: str | None = None
    capture_mode: CaptureMode = "disabled"

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def discard_noncanonical_context_fields(cls, value: Any) -> Any:
        """Ignore old body fields without using them as correlation inputs.

        This keeps the receiver compatible with the deployed API during the
        rollout. The HTTP header remains the only source of observability_id.
        """

        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        # LEGACY COMPATIBILITY: API v1 sent submission_claim_id as process_id.
        # Remove this mapping after the API v1 deployment and rollback period.
        legacy_process_id = normalized.pop("process_id", None)
        if "submission_claim_id" not in normalized and legacy_process_id is not None:
            normalized["submission_claim_id"] = legacy_process_id
        return {
            key: item
            for key, item in normalized.items()
            if key not in NONCANONICAL_CONTEXT_FIELDS
        }


def remote_context(params: dict[str, Any]) -> dict[str, str] | None:
    """Return only the correlation fields accepted by the shared runtime."""

    context = params.get("_observability_context")
    if not isinstance(context, dict):
        return None
    try:
        validated = ObservabilityContext.model_validate(context)
    except ValidationError:
        return None
    return {
        key: str(value)
        for key, value in validated.model_dump(exclude_none=True).items()
    }


def apply_remote_context(
    runtime: Any,
    params: dict[str, Any],
) -> dict[str, str] | None:
    """Validate remote context and apply its diagnostic identifier locally."""

    propagated = remote_context(params)
    if propagated is not None:
        observability_id = propagated.get("observability_id")
        if observability_id is not None:
            runtime.set_context(observability_id=observability_id)
    return propagated


def split_internal_payload(
    params: dict[str, Any],
) -> tuple[dict[str, Any], TelemetryEnvelope | None, dict[str, Any] | None]:
    simulation_params = dict(params)
    raw_telemetry = simulation_params.pop("_telemetry", None)
    simulation_params.pop("_observability_context", None)
    raw_metadata = simulation_params.pop("_metadata", None)

    telemetry = None
    if raw_telemetry is not None:
        try:
            telemetry = TelemetryEnvelope.model_validate(raw_telemetry)
        except ValidationError:
            telemetry = None

    metadata = raw_metadata if isinstance(raw_metadata, dict) else None
    return simulation_params, telemetry, metadata
