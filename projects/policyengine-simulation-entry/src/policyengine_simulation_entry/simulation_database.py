"""The simulation service's connection to the Public API's database."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from policyengine_simulation_persistence import create_simulation_engine
from sqlalchemy import select

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

    def _check_connection(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(select(1))

    async def check_connection(self) -> None:
        """Raise unless a pooled connection answers a trivial query."""
        await asyncio.to_thread(self._check_connection)

    def dispose(self) -> None:
        """Close every pooled connection."""
        self.engine.dispose()
