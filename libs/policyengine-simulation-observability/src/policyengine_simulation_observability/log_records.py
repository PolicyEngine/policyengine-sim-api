"""Telemetry fields that name an API v1 row, and log fields that are emitted.

``policyengine-observability``'s standard-library log handler emits only a
log record's ``policyengine_attributes``; every other key passed in
``extra=`` stays on the standard-library record alone. It then drops each
attribute whose key contains ``household``, ``reform``, ``payload`` or
``person``, among other words. So a row is named by the table holding it
(``database_table``) and its UUID (``database_row_id``), never by a key such
as ``household_job_id``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from uuid import UUID

DATABASE_TABLE_ATTRIBUTE = "database_table"
DATABASE_ROW_ID_ATTRIBUTE = "database_row_id"
MODAL_CALL_ID_ATTRIBUTE = "modal_call_id"

# The API v1 tables whose rows telemetry names.
DatabaseTable = Literal[
    "household_jobs", "report_runs", "reports", "simulation_runs", "simulations"
]
LogFieldValue = str | int | float | bool

# ``logging`` raises on an ``extra`` key naming one of a record's own
# attributes, or the two it sets while formatting.
_LOG_RECORD_ATTRIBUTES: Final = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))
) | {"message", "asctime"}


def database_row_attributes(table: DatabaseTable, row_id: UUID) -> dict[str, str]:
    """Return the attributes naming row ``row_id`` of API v1's ``table``."""
    return {DATABASE_TABLE_ATTRIBUTE: table, DATABASE_ROW_ID_ATTRIBUTE: str(row_id)}


def log_extra(**fields: LogFieldValue) -> dict[str, object]:
    """Return a logging call's ``extra`` whose ``fields`` reach the emitted record.

    Each field also stays on the standard-library record, where other
    handlers and tests read it, unless its name is one of the record's own
    attributes: ``logging`` would raise for that one, and these calls are
    often in error handlers. A field must be a string, a number or a
    boolean: the runtime drops anything else.
    """
    record_fields = {
        name: value
        for name, value in fields.items()
        if name not in _LOG_RECORD_ATTRIBUTES
    }
    return {**record_fields, "policyengine_attributes": dict(fields)}
