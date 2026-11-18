"""Proof fixture: log calls whose fields never reach Cloud Logging.

The rule must reject both. The first is a real past mistake: until this rule,
the Simulation Entrypoint's household job logs passed `household_job_id` as a
plain `extra=` key, which the observability runtime's log handler ignores. The
second passes the key through `log_extra`, but the runtime drops it by name.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def record_call_failed(job_id: str, error: Exception) -> None:
    """Log a call ID that could not be recorded, with plain extra fields."""
    logger.warning(
        "household_call_not_recorded",
        extra={"household_job_id": job_id, "error_type": type(error).__name__},
    )


def fail_unconfirmed(log_extra: Callable[..., dict[str, object]], job_id: str) -> None:
    """Log an unconfirmed call, naming the job by a key the runtime drops."""
    logger.error("household_call_unconfirmed", extra=log_extra(household_job_id=job_id))
