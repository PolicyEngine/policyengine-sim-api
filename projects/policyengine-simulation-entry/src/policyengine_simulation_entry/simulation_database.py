"""The simulation service's connection to the Public API's database."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from policyengine_simulation_persistence import (
    create_simulation_engine,
    missing_table_privileges,
)
from sqlalchemy import func, select

if TYPE_CHECKING:
    from sqlalchemy import Engine

# The readiness check stops waiting after three seconds, but it can't stop the
# thread its query runs on, so the database ends the query sooner.
READINESS_STATEMENT_TIMEOUT_MILLISECONDS = 2000


class SimulationDatabase:
    """Run short, blocking database operations off the event loop."""

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, which connects as the simulation service's role."""
        self.engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> SimulationDatabase:
        """Build the runtime engine; no connection opens until first use.

        Raise ValueError, without echoing the URL, when it is malformed.
        """
        return cls(create_simulation_engine(database_url))

    def _missing_privileges(self) -> list[str]:
        with self.engine.connect() as connection, connection.begin():
            # Local to the transaction, so it works through Supabase's
            # transaction pooler and never outlives this check.
            connection.execute(
                select(
                    func.set_config(
                        "statement_timeout",
                        str(READINESS_STATEMENT_TIMEOUT_MILLISECONDS),
                        True,  # noqa: FBT003  # set_config's is_local argument
                    )
                )
            )
            return missing_table_privileges(connection)

    async def missing_privileges(self) -> list[str]:
        """Return each table privilege the service needs and its role lacks."""
        return await asyncio.to_thread(self._missing_privileges)

    def dispose(self) -> None:
        """Close every pooled connection."""
        self.engine.dispose()
