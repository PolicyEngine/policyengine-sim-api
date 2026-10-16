from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self

from .stages import TracerCaptureMode


def parse_header_value_pairs(raw: str | None) -> dict[str, str]:
    """Parse OTLP headers from a comma or newline separated key=value string."""

    if raw is None:
        return {}

    stripped = raw.strip()
    if not stripped:
        return {}

    headers: dict[str, str] = {}
    for pair in stripped.replace("\n", ",").split(","):
        candidate = pair.strip()
        if not candidate:
            continue
        key, separator, value = candidate.partition("=")
        if not separator:
            raise ValueError(
                "Expected OTLP headers in key=value format separated by commas"
            )
        headers[key.strip()] = value.strip()

    return headers


@dataclass(frozen=True)
class ObservabilityConfig:
    enabled: bool = False
    shadow_mode: bool = True
    service_name: str = "policyengine-observability"
    environment: str = "production"
    otlp_endpoint: str | None = None
    otlp_headers: dict[str, str] = field(default_factory=dict)
    artifact_bucket: str | None = None
    artifact_prefix: str = "simulation-observability"
    tracer_capture_mode: TracerCaptureMode = TracerCaptureMode.DISABLED
    slow_run_threshold_seconds: float = 30.0

    @classmethod
    def disabled(cls, service_name: str = "policyengine-observability") -> Self:
        return cls(enabled=False, service_name=service_name)
