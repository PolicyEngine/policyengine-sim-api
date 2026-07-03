"""Helpers for safe gateway error reporting.

We intentionally do **not** leak ``str(exc)`` to API callers. Worker-side
exceptions routinely embed container paths, HF URLs with pre-signed tokens,
and internal parameter names that we do not want to surface to the public
internet. Instead, we record the full exception server-side, then return the
caller a stable generic message plus a correlation id they can cite to
support.

The stdlib logger is the guaranteed server-side sink: it always fires, so the
correlation id in the caller-facing message always maps to a log line with the
full message and stack trace. ``policyengine-observability`` and legacy
Logfire (kept while we evaluate replacing it) are best-effort on top — both
silently no-op when their runtime is disabled or unconfigured.

Two helpers live here:

- :func:`log_and_redact_exception` for request-handler code paths that want
  to surface an HTTP error body.
- :func:`make_correlation_id` for anywhere we need a free-standing id that
  can be stitched back to a logged span.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from policyengine_observability import record_error, record_event
from src.modal.logfire_legacy import (
    legacy_logfire_attributes,
    logfire_is_configured,
)

logger = logging.getLogger(__name__)


try:
    import logfire as _logfire  # type: ignore
except Exception:  # pragma: no cover - logfire optional locally
    _logfire = None


GENERIC_JOB_FAILURE_MESSAGE = "Simulation failed"


def make_correlation_id() -> str:
    return uuid.uuid4().hex


def log_and_redact_exception(
    exc: BaseException,
    *,
    scope: str,
    context: dict[str, Any] | None = None,
) -> str:
    """Record ``exc`` with full context server-side and return a redacted
    caller-safe message paired with a correlation id.

    The public return string has the shape ``"Simulation failed
    (correlation_id=<hex>)"`` so clients can paste it into support tickets
    without needing to parse JSON. The same correlation id is attached to
    the server-side log line so operators can jump between the two.
    """

    correlation_id = make_correlation_id()
    payload = {
        "correlation_id": correlation_id,
        "scope": scope,
        "error_type": type(exc).__name__,
        **legacy_logfire_attributes(),
    }
    if context:
        payload.update(context)

    # Guaranteed sink. record_error/record_event silently no-op when the
    # observability runtime is disabled or unconfigured (they never raise),
    # so this stdlib line is what makes the correlation id in the caller's
    # message always resolvable to a full message + stack server-side.
    try:
        logger.error(
            "Gateway %s failed (correlation_id=%s)",
            scope,
            correlation_id,
            exc_info=exc,
            extra=payload,
        )
    except Exception:  # pragma: no cover - context key clashed with LogRecord
        logger.error(
            "Gateway %s failed (correlation_id=%s)",
            scope,
            correlation_id,
            exc_info=exc,
        )

    try:
        record_error(exc, handled=True, status_code=500)
        record_event(
            "gateway_error_redacted",
            **payload,
        )
    except Exception:  # pragma: no cover - defensive, never raise from logger
        pass

    if logfire_is_configured():
        try:
            _logfire.exception(  # type: ignore[union-attr]
                "Gateway {scope} failed",
                **payload,
            )
        except Exception:  # pragma: no cover - defensive, never raise from logger
            pass

    return f"{GENERIC_JOB_FAILURE_MESSAGE} (correlation_id={correlation_id})"
