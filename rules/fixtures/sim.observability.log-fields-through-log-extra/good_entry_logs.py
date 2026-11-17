"""Proof fixture: a log call whose fields reach Cloud Logging.

The rule must accept it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from policyengine_simulation_observability.log_records import (
    database_row_attributes,
    log_extra,
)

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)


def record_call_failed(job_id: UUID, error: Exception) -> None:
    """Log a call ID that could not be recorded, naming the job's row."""
    logger.warning(
        "household_call_not_recorded",
        extra=log_extra(
            error_type=type(error).__name__,
            **database_row_attributes("household_jobs", job_id),
        ),
    )
