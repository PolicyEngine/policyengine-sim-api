"""The Simulation Entrypoint's connection to its own ``simulation`` schema."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from policyengine_simulation_persistence import (
    create_simulation_engine,
    read_schema_revision,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine


class SimulationDatabase:
    """Run short, blocking database operations off the event loop."""

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, which connects as the runtime role."""
        self.engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> SimulationDatabase:
        """Build the runtime engine; no connection opens until first use.

        Raise ValueError, without echoing the URL, when it is malformed.
        """
        return cls(create_simulation_engine(database_url))

    def _read_revision(self) -> str | None:
        with self.engine.connect() as connection:
            return read_schema_revision(connection)

    async def read_revision(self) -> str | None:
        """Return the applied migration revision, or None before the first one."""
        return await asyncio.to_thread(self._read_revision)

    def dispose(self) -> None:
        """Close every pooled connection."""
        self.engine.dispose()
