"""Payload redaction helpers for structured logs and legacy Logfire spans.

We keep these in a separate module from :mod:`src.modal.app` so they can be
unit-tested without instantiating the Modal app (which requires runtime
Modal configuration that unit tests don't provide).
"""

from __future__ import annotations


# Keys in request payloads that must never reach observability backends.
# ``data`` is a signed GCS/HF URL (may contain embedded short-lived
# credentials), the reform/baseline parameter trees are potentially large
# and reveal proprietary policy design, and ``_telemetry`` / ``_metadata``
# hold internal routing and correlation fields we log separately via
# dedicated structured attributes.
SENSITIVE_PARAM_KEYS = ("data", "reform", "baseline", "_telemetry", "_metadata")


def redact_params_for_logging(params) -> dict:
    """Return a shallow copy of ``params`` safe for observability attributes.

    We preserve routing-relevant fields (country, scope, version, time
    period, etc.) so operators can still trace which simulation a span
    corresponds to, but strip any field that may contain URLs with signed
    credentials or arbitrarily large user-submitted parameter trees.
    Non-dict inputs return an empty dict so callers can splat the result
    into operation attributes without additional guards.
    """

    if not isinstance(params, dict):
        return {}
    redacted = {
        key: value for key, value in params.items() if key not in SENSITIVE_PARAM_KEYS
    }
    # Surface only the correlation/run ids from telemetry, not the whole
    # envelope.
    telemetry = params.get("_telemetry")
    if isinstance(telemetry, dict):
        run_id = telemetry.get("run_id")
        if run_id is not None:
            redacted["run_id"] = run_id
    return redacted
