"""Allowlisted correlation metadata for asynchronous simulation dispatch."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator


CaptureMode = Literal["disabled", "failures", "threshold", "sampled", "always"]


class ObservabilityContext(BaseModel):
    traceparent: str | None = None
    tracestate: str | None = None
    captured_at: datetime
    request_id: str | None = None
    job_id: str | None = None
    observability_id: str | None = None
    simulation_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class TelemetryEnvelope(BaseModel):
    """Bounded internal metadata passed from API request to Modal worker."""

    observability_id: str | None = None
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
    def normalize_previous_api_fields(cls, value: Any) -> Any:
        """Accept the production API envelope during the rolling deployment."""

        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for previous_name, current_name in (
            ("run_id", "observability_id"),
            ("process_id", "submission_claim_id"),
        ):
            if previous_name not in normalized:
                continue
            previous_value = normalized.pop(previous_name)
            current_value = normalized.get(current_name)
            if current_value is not None and previous_value != current_value:
                raise ValueError(
                    f"{previous_name} and {current_name} must match when both are set"
                )
            if current_value is None:
                normalized[current_name] = previous_value

        # These values now travel in W3C headers and _observability_context.
        # Accept and remove them while the current API revision remains live.
        normalized.pop("request_id", None)
        normalized.pop("traceparent", None)
        return normalized


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
