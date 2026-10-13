"""Proof fixture: a Stage 12 runtime that creates its own table. The rule must reject it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Connection


def ensure_comparison_table(connection: Connection) -> None:
    connection.execute(text("CREATE TABLE stage12_evaluation_reports (id uuid)"))
