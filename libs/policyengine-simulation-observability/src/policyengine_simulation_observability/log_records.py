"""Telemetry fields that name a Public API row, and log fields that are emitted.

``policyengine-observability``'s standard-library log handler emits only a
log record's ``policyengine_attributes``; every other key passed in
``extra=`` stays on the standard-library record alone. It then drops each
attribute whose key contains ``household``, ``reform``, ``payload`` or
``person``, among other words. So a row is named by the table holding it
(``database_table``) and its UUID (``database_row_id``), never by a key such
as ``household_job_id``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

DATABASE_TABLE_ATTRIBUTE = "database_table"
DATABASE_ROW_ID_ATTRIBUTE = "database_row_id"
MODAL_CALL_ID_ATTRIBUTE = "modal_call_id"

LogFieldValue = str | int | float | bool


def database_row_attributes(table: str, row_id: UUID) -> dict[str, str]:
    """Return the attributes naming row ``row_id`` of the Public API's ``table``."""
    return {DATABASE_TABLE_ATTRIBUTE: table, DATABASE_ROW_ID_ATTRIBUTE: str(row_id)}


def log_extra(**fields: LogFieldValue) -> dict[str, object]:
    """Return a logging call's ``extra`` whose ``fields`` reach the emitted record.

    Each field also stays on the standard-library record, where other
    handlers and tests read it. A field must be a string, a number or a
    boolean: the runtime drops anything else.
    """
    return {**fields, "policyengine_attributes": dict(fields)}
