"""The simulation service's connection to the Public API's database."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from policyengine_simulation_persistence import (
    create_simulation_engine,
    missing_table_privileges,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine


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
        with self.engine.connect() as connection:
            return missing_table_privileges(connection)

    async def missing_privileges(self) -> list[str]:
        """Return each table privilege the service needs and its role lacks."""
        return await asyncio.to_thread(self._missing_privileges)

    def dispose(self) -> None:
        """Close every pooled connection."""
        self.engine.dispose()
