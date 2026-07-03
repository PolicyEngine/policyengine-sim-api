"""
Internal telemetry helpers for Modal request passthrough.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


CaptureMode = Literal["disabled", "failures", "threshold", "sampled", "always"]


class TelemetryEnvelope(BaseModel):
    """Minimal shared telemetry payload shape for gateway and worker code."""

    run_id: str
    process_id: str | None = None
    request_id: str | None = None
    traceparent: str | None = None
    requested_at: datetime | None = None
    simulation_kind: str | None = None
    geography_code: str | None = None
    geography_type: str | None = None
    config_hash: str | None = None
    capture_mode: CaptureMode = "disabled"

    model_config = ConfigDict(extra="forbid")


def split_internal_payload(
    params: dict[str, Any],
) -> tuple[dict[str, Any], TelemetryEnvelope | None, dict[str, Any] | None]:
    """Strip internal passthrough fields before SimulationOptions validation."""

    simulation_params = dict(params)
    raw_telemetry = simulation_params.pop("_telemetry", None)
    raw_metadata = simulation_params.pop("_metadata", None)

    telemetry = None
    if raw_telemetry is not None:
        telemetry = TelemetryEnvelope.model_validate(raw_telemetry)

    metadata = raw_metadata if isinstance(raw_metadata, dict) else None
    return simulation_params, telemetry, metadata
