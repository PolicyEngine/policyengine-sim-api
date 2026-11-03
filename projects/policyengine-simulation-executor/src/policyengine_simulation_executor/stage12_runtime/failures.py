"""Safe, structured failure details for Stage 12 execution."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, NoReturn

from policyengine_simulation_contract.input_errors import input_error_detail
from policyengine_simulation_observability.errors import make_correlation_id
from pydantic import BaseModel, ConfigDict

from .failure_causes import StageProgress, failure_summary

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
        """Keep the safe ``code`` and ``message`` for the caller."""
        self.code = code
        self.message = message
        super().__init__(message)


class Stage12ExecutionError(RuntimeError):
    """Carry one safe failure between Stage 12 execution layers."""

    def __init__(self, detail: Stage12FailureDetail) -> None:
        """Carry ``detail``; the exception's own message is fixed and generic."""
        self.detail = detail
        super().__init__("Stage 12 execution failed")


class Stage12CancellationError(RuntimeError):
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


def redacted_failure_detail(
    error: BaseException,
    *,
    default_code: str,
    segment_index: int | None = None,
    progress: StageProgress | None = None,
) -> Stage12FailureDetail:
    """Retain typed public failures and redact unexpected exception messages.

    This only describes the failure; it does not report it. Any other error
    is summarized by its redacted cause (see ``failure_summary``), naming the
    stage ``progress`` recorded, and a new correlation ID. The caller reports
    the error once with that ID so the summary leads back to it. The cause is
    shortened, never the ID, to fit the summary limit.
    """
    if isinstance(error, Stage12ExecutionError):
        return error.detail
    if isinstance(error, Stage12InputError):
        return _safe_detail(
            error_code=error.code,
            error_summary=error.message,
            segment_index=segment_index,
        )
    input_error = input_error_detail(error)
    if input_error is not None:
        return _safe_detail(
            error_code=input_error.code,
            error_summary=input_error.message,
            segment_index=segment_index,
        )

    correlation_id = make_correlation_id()
    correlation = f" (correlation_id={correlation_id})"
    cause = failure_summary(error, progress=progress)
    if segment_index is not None:
        cause = f"Segment {segment_index}: {cause}"
    cause = _bounded(cause, ERROR_SUMMARY_MAX_LENGTH - len(correlation))
    return _safe_detail(
        error_code=default_code,
        error_summary=f"{cause}{correlation}",
        correlation_id=correlation_id,
        segment_index=segment_index,
    )


# A date-range period is a start and an end date joined by a dot.
_DATE_RANGE_PART_COUNT = 2


def validate_policy_periods(policy: Mapping[str, Any]) -> None:
    """Require effective dates in the wire formats emitted by the frontend."""
    for parameter, value in policy.items():
        if not isinstance(value, Mapping):
            continue
        for raw_period in value:
            period = str(raw_period)
            parts = period.split(".")
            if len(parts) not in {1, _DATE_RANGE_PART_COUNT}:
                _raise_invalid_period(parameter, period)
            parsed = [_parse_period_date(parameter, period, part) for part in parts]
            if len(parsed) == _DATE_RANGE_PART_COUNT and parsed[1] < parsed[0]:
                _raise_invalid_period(parameter, period)


def _parse_period_date(parameter: str, period: str, part: str) -> date:
    try:
        parsed = date.fromisoformat(part)
    except ValueError:
        _raise_invalid_period(parameter, period)
    return parsed


def _raise_invalid_period(parameter: str, period: str) -> NoReturn:
    msg = "invalid_policy_period"
    raise Stage12InputError(
        msg,
        f"Policy parameter {parameter!r} has invalid effective period "
        f"{period!r}; expected YYYY-MM-DD or "
        "YYYY-MM-DD.YYYY-MM-DD.",
    )
