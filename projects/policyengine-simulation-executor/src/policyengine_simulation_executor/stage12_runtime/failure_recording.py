"""Failure reporting that does not depend on the Stage 12 database."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from policyengine_observability import ObservabilityRuntime

    from .failure_causes import StageProgress

from .failures import Stage12FailureDetail, redacted_failure_detail

logger = logging.getLogger(__name__)


def report_failure(
    error: Exception,
    *,
    runtime: ObservabilityRuntime | None,
    failure_scope: str,
    default_code: str,
    attributes: Mapping[str, object] | None = None,
    segment_index: int | None = None,
    progress: StageProgress | None = None,
) -> Stage12FailureDetail:
    """Report a failure once and return the safe detail to persist for it.

    The report is emitted before any database write is attempted, as
    ``report_original_failure`` requires. An unexpected error's detail has a
    new correlation ID, and the report carries the same ID, so the redacted
    summary persisted for callers leads back to this one record of the
    original error. ``progress`` names the failing stage in that summary.
    """
    detail = redacted_failure_detail(
        error,
        default_code=default_code,
        segment_index=segment_index,
        progress=progress,
    )
    correlation = (
        {"correlation_id": detail.correlation_id}
        if detail.correlation_id is not None
        else {}
    )
    report_original_failure(
        error,
        runtime=runtime,
        failure_scope=failure_scope,
        attributes={**(attributes or {}), **correlation},
    )
    return detail


def report_original_failure(
    error: Exception,
    *,
    runtime: ObservabilityRuntime | None,
    failure_scope: str,
    attributes: Mapping[str, object] | None = None,
) -> None:
    """Emit the original failure before any database write is attempted.

    Recording a failure in the database can itself fail, for example when the
    session pooler has no free connections. Telemetry therefore receives the
    original cause first, so it survives a failed or hung database write.
    """
    _emit_error(
        "Stage 12 execution failed",
        error=error,
        runtime=runtime,
        attributes={"stage12_failure_scope": failure_scope, **(attributes or {})},
    )


def persist_failure_record[WriteResult](
    write_failure_record: Callable[[], WriteResult],
    *,
    runtime: ObservabilityRuntime | None,
    failure_scope: str,
) -> WriteResult | None:
    """Attempt a failure write without letting its error replace the original.

    Returns what the write returned, or ``None`` when the write failed. The
    caller raises the same failure after this returns, whether or not the
    record was written: a child handler re-raises the original error, and the
    report and simulation handlers raise their stable ``RuntimeError``. The
    original exception then remains visible only in telemetry; the record
    holds its redacted summary.
    """
    try:
        return write_failure_record()
    except Exception as persistence_error:  # noqa: BLE001
        _emit_error(
            "Stage 12 failure record could not be persisted",
            error=persistence_error,
            runtime=runtime,
            attributes={"stage12_failure_scope": failure_scope},
        )
        return None


def _emit_error(
    message: str,
    *,
    error: Exception,
    runtime: ObservabilityRuntime | None,
    attributes: Mapping[str, object],
) -> None:
    if runtime is None:
        failure_label = str(attributes.get("stage12_failure_scope"))
        correlation_id = attributes.get("correlation_id")
        if correlation_id is not None:
            failure_label = f"{failure_label}; correlation_id={correlation_id}"
        logger.error(
            "%s (%s)",
            message,
            failure_label,
            exc_info=error,
        )
        return
    # Telemetry must not change the failure the caller propagates.
    with suppress(Exception):
        runtime.log(message, severity="ERROR", attributes=attributes, error=error)
