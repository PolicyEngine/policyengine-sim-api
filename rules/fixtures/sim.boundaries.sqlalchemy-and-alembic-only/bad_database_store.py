"""Proof fixture: a store that bypasses SQLAlchemy and creates its own tables.

The rule must reject it. The driver connection is a real past mistake: commit
08ebe1ca added a Stage 12 persistence adapter to policyengine-simulation-contract
that imported psycopg and opened its connections with psycopg.connect, until
commit 7e7fa003 replaced it with a SQLAlchemy engine. The store below is that
adapter's connection code, shortened. The metadata.create_all call is
illustrative, not taken from history: no commit in this repository has called
it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row
from sqlalchemy import MetaData, create_engine

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

REPORT_SELECT_SQL = (
    "SELECT * FROM public.stage12_evaluation_reports WHERE evaluation_id = %(id)s"
)

metadata = MetaData()


class PostgresEvaluationStore:
    """Read temporary records over a connection the driver opens directly."""

    def __init__(
        self,
        database_url: str,
        *,
        connect: Callable[..., Any] = psycopg.connect,
    ) -> None:
        """Keep the database URL and the driver function that connects to it."""
        self._database_url = database_url
        self._connect = connect

    def _connection(self) -> psycopg.Connection[dict[str, Any]]:
        return self._connect(self._database_url, row_factory=dict_row)

    def get_report(self, evaluation_id: UUID) -> dict[str, Any] | None:
        """Read one report row through a driver cursor."""
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(REPORT_SELECT_SQL, {"id": evaluation_id})
            return cursor.fetchone()


def ensure_tables(database_url: str) -> None:
    """Create every mapped table from runtime code, the schema change to catch."""
    engine = create_engine(database_url)
    metadata.create_all(engine)
