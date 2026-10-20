"""Shared helpers for parent jobs that fan out child simulation requests.

Both fan-out orchestrators (budget-window batches and segmented national
runs) build child payloads the same way: copy the parent's raw params minus
parent-scoped fields, overwrite the fan-out axis, and re-attach
``_telemetry`` so child spans join the parent's trace. Keeping the rule in
one place stops the builders drifting (e.g. an internal key stripped in one
orchestrator but forwarded by the other).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable


def build_child_payload(
    raw_params: dict[str, Any],
    *,
    strip_fields: Iterable[str],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Copy parent params minus ``strip_fields`` and apply the child ``overrides``.

    ``_telemetry`` is always re-attached from the parent (when it is a dict),
    even if ``overrides`` sets it, so children join the parent's trace.
    """
    strip = set(strip_fields) | {"_telemetry"}
    payload = {key: value for key, value in raw_params.items() if key not in strip}
    payload.update(overrides)
    telemetry = raw_params.get("_telemetry")
    if isinstance(telemetry, dict):
        payload["_telemetry"] = telemetry
    return payload


@dataclass(frozen=True)
class PollBackoff:
    """How a parent job spaces its polls of child calls that are still running.

    Sleeps start at ``initial_seconds`` and grow by ``factor``, up to
    ``maximum_seconds``, while no child finishes.
    """

    initial_seconds: float
    maximum_seconds: float
    factor: float


def next_backoff(current: float, *, factor: float, maximum: float) -> float:
    """Return the next sleep in an exponential backoff walk, capped at ``maximum``."""
    return min(current * factor, maximum)
