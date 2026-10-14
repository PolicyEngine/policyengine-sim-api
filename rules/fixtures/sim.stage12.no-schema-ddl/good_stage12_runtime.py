"""Proof fixture: a Stage 12 runtime that writes rows with typed DML. The rule must accept it."""

from sqlalchemy import insert


def record_report(connection, reports_table, report_id: str) -> None:
    connection.execute(insert(reports_table).values(id=report_id))
