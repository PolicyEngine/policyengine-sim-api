"""Name the database row and Modal call of a v2 Modal function's operation.

Each v2 Modal function opens one telemetry operation. Its attributes reach
the operation's log records and span, but not its metrics, so an operation
that Modal interrupted in production leads straight to the Public API row it
was working on:

- ``database_table``: the table holding that row, ``household_jobs``,
  ``report_runs`` (a report run, or a household report run) or
  ``simulation_runs``;
- ``database_row_id``: the row's UUID;
- ``modal_call_id``: the function's own Modal function call ID.

The observability package drops every attribute whose key contains
``household``, among other words, so no key here names one. A call that
works on no detailed-save row (a comparison-path call, a standalone
simulation, a release validation's household calculation) names only its
Modal call.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from policyengine_simulation_observability.log_records import (
    MODAL_CALL_ID_ATTRIBUTE,
    database_row_attributes,
)

DatabaseTable = Literal["household_jobs", "report_runs", "simulation_runs"]


def operation_record_attributes(
    table: DatabaseTable,
    row_id: UUID | None,
    modal_call_id: str | None,
) -> dict[str, str]:
    """Return the operation attributes naming ``row_id`` in ``table`` and the call.

    A missing row or Modal call ID is left out.
    """
    attributes: dict[str, str] = (
        {} if row_id is None else database_row_attributes(table, row_id)
    )
    if modal_call_id:
        attributes[MODAL_CALL_ID_ATTRIBUTE] = modal_call_id
    return attributes


def row_id_or_none(value: object) -> UUID | None:
    """Return ``value`` as a UUID, or None when it is missing or not a UUID.

    Telemetry names only a well-formed ID; the function refuses a malformed
    one itself.
    """
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def report_run_named_by(context: object) -> UUID | None:
    """Return the report run a coordinator call's context names, if any."""
    if not isinstance(context, Mapping):
        return None
    report_run = context.get("report_run")
    if not isinstance(report_run, Mapping):
        return None
    return row_id_or_none(report_run.get("report_run_id"))


def simulation_run_named_by(context: object) -> UUID | None:
    """Return the simulation run a simulation worker call's context names, if any."""
    if not isinstance(context, Mapping):
        return None
    return row_id_or_none(context.get("simulation_run_id"))
