"""Proof fixture: a Stage 12 runtime that creates its own table. The rule must reject it."""

from sqlalchemy import text


def ensure_comparison_table(connection) -> None:
    connection.execute(text("CREATE TABLE stage12_evaluation_reports (id uuid)"))
