"""Safe, structured failure details for Stage 12 execution."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import logging
from typing import Any

from policyengine_observability import ObservabilityRuntime
from policyengine_simulation_contract.spm import spm_error_detail
from pydantic import BaseModel, ConfigDict

from policyengine_simulation_observability.errors import (
    GENERIC_JOB_FAILURE_MESSAGE,
    log_and_redact_exception,
    make_correlation_id,
)

logger = logging.getLogger(__name__)

ERROR_CODE_MAX_LENGTH = 64
ERROR_SUMMARY_MAX_LENGTH = 512


def _bounded(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    return value[: maximum - 3] + "..."


class Stage12FailureDetail(BaseModel):
    """Failure values safe to persist and return from the internal status API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error_code: str
    error_summary: str
    correlation_id: str | None = None
    segment_index: int | None = None


class Stage12InputError(ValueError):
    """An input error whose code and message are safe to return to the caller."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class Stage12ExecutionError(RuntimeError):
    """Carry one safe failure between Stage 12 execution layers."""

    def __init__(self, detail: Stage12FailureDetail) -> None:
        self.detail = detail
        super().__init__("Stage 12 execution failed")


class Stage12Cancellation(RuntimeError):
    """Stop a simulation whose durable record was cancelled by its parent."""


def _safe_detail(
    *,
    error_code: str,
    error_summary: str,
    correlation_id: str | None = None,
    segment_index: int | None = None,
) -> Stage12FailureDetail:
    return Stage12FailureDetail(
        error_code=_bounded(error_code, ERROR_CODE_MAX_LENGTH),
        error_summary=_bounded(error_summary, ERROR_SUMMARY_MAX_LENGTH),
        correlation_id=correlation_id,
        segment_index=segment_index,
    )


def failure_detail_from_record(
    *,
    error_code: str,
    error_summary: str | None,
) -> Stage12FailureDetail:
    """Recreate a propagated failure without replacing its persisted values."""

    return _safe_detail(
        error_code=error_code,
        error_summary=error_summary or "Simulation failed",
    )


def failure_detail_from_exception(
    error: BaseException,
    *,
    runtime: ObservabilityRuntime | None,
    scope: str,
    default_code: str,
    context: dict[str, Any] | None = None,
    segment_index: int | None = None,
) -> Stage12FailureDetail:
    """Retain typed public failures and redact unexpected exception messages."""

    if isinstance(error, Stage12ExecutionError):
        return error.detail
    if isinstance(error, Stage12InputError):
        return _safe_detail(
            error_code=error.code,
            error_summary=error.message,
            segment_index=segment_index,
        )
    spm = spm_error_detail(error)
    if spm is not None:
        return _safe_detail(
            error_code=spm.code,
            error_summary=spm.message,
            segment_index=segment_index,
        )

    if runtime is not None:
        summary = log_and_redact_exception(
            error,
            runtime=runtime,
            scope=scope,
            context=context,
        )
        correlation_id = summary.removeprefix(
            f"{GENERIC_JOB_FAILURE_MESSAGE} (correlation_id="
        ).removesuffix(")")
    else:
        correlation_id = make_correlation_id()
        logger.error(
            "Stage 12 %s failed (correlation_id=%s)",
            scope,
            correlation_id,
            exc_info=error,
            extra={"correlation_id": correlation_id, **(context or {})},
        )
        summary = f"{GENERIC_JOB_FAILURE_MESSAGE} (correlation_id={correlation_id})"
    if segment_index is not None:
        summary = f"Segment {segment_index}: {summary}"
    return _safe_detail(
        error_code=default_code,
        error_summary=summary,
        correlation_id=correlation_id,
        segment_index=segment_index,
    )


def validate_policy_periods(policy: Mapping[str, Any]) -> None:
    """Require effective dates in the wire formats emitted by the frontend."""

    for parameter, value in policy.items():
        if not isinstance(value, Mapping):
            continue
        for raw_period in value:
            period = str(raw_period)
            parts = period.split(".")
            if len(parts) not in {1, 2}:
                _raise_invalid_period(parameter, period)
            parsed = []
            for part in parts:
                try:
                    parsed.append(date.fromisoformat(part))
                except ValueError:
                    _raise_invalid_period(parameter, period)
            if len(parsed) == 2 and parsed[1] < parsed[0]:
                _raise_invalid_period(parameter, period)


def _raise_invalid_period(parameter: str, period: str) -> None:
    raise Stage12InputError(
        "invalid_policy_period",
        f"Policy parameter {parameter!r} has invalid effective period "
        f"{period!r}; expected YYYY-MM-DD or "
        "YYYY-MM-DD.YYYY-MM-DD.",
    )
