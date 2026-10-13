"""Proof fixture: a Stage 12 runtime that writes rows with typed DML.

The rule must accept it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import insert

if TYPE_CHECKING:
    from sqlalchemy import Connection, Table


def record_report(connection: Connection, reports_table: Table, report_id: str) -> None:
    """Insert one report row into a table that policyengine-api owns."""
    connection.execute(insert(reports_table).values(id=report_id))
