"""Shared retry behavior for deployed simulation status polling."""

import math
from collections.abc import Mapping
from http import HTTPStatus


TRANSIENT_POLL_STATUS_CODES: frozenset[HTTPStatus] = frozenset(
    {
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
    }
)


def poll_retry_delay_seconds(
    status_code: HTTPStatus,
    headers: Mapping[str, str],
    *,
    fallback_seconds: float,
) -> float | None:
    """Return a bounded-polling delay for a retryable status response."""
    if status_code == HTTPStatus.ACCEPTED:
        return fallback_seconds
    if status_code not in TRANSIENT_POLL_STATUS_CODES:
        return None

    retry_after = next(
        (value for name, value in headers.items() if name.lower() == "retry-after"),
        None,
    )
    if retry_after is None:
        return fallback_seconds

    try:
        delay = float(retry_after)
    except ValueError:
        return fallback_seconds

    if not math.isfinite(delay) or delay < 0:
        return fallback_seconds
    return delay
