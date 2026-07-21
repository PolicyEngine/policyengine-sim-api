"""Shared helpers for parent jobs that fan out child simulation requests.

Both fan-out orchestrators (budget-window batches and segmented national
runs) build child payloads the same way: copy the parent's raw params minus
parent-scoped fields, overwrite the fan-out axis, and re-attach
``_telemetry`` so child spans join the parent's trace. Keeping the rule in
one place stops the builders drifting (e.g. an internal key stripped in one
orchestrator but forwarded by the other).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def build_child_payload(
    raw_params: dict[str, Any],
    *,
    strip_fields: Iterable[str],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    strip = set(strip_fields) | {"_telemetry"}
    payload = {key: value for key, value in raw_params.items() if key not in strip}
    payload.update(overrides)
    telemetry = raw_params.get("_telemetry")
    if isinstance(telemetry, dict):
        payload["_telemetry"] = telemetry
    return payload


def next_backoff(current: float, *, factor: float, maximum: float) -> float:
    """The next sleep in an exponential backoff walk."""
    return min(current * factor, maximum)
