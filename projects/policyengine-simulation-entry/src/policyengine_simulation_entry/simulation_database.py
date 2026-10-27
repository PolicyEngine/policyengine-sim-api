"""The Simulation Entrypoint's connection to its own ``simulation`` schema."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from policyengine_simulation_persistence import (
    RunStore,
    RunTransitions,
    create_simulation_engine,
    read_schema_revision,
)

from policyengine_simulation_entry.economy_recorders import economy_result_recorders
from policyengine_simulation_entry.modal_calls import ModalCallInspector
from policyengine_simulation_entry.run_recording import RunRecorder

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

    def call_recorder(self) -> RunRecorder:
        """Return what records a reported call's outcome on its runs.

        Economy simulation runs and comparison steps have result recorders. A
        finished call of another kind is answered no_result_recorder and left
        for a revision that records it.
        """
        return RunRecorder(
            transitions=RunTransitions(self.engine),
            runs=RunStore(self.engine),
            inspector=ModalCallInspector(),
            recorders=economy_result_recorders(),
        )

    def dispose(self) -> None:
        """Close every pooled connection."""
        self.engine.dispose()
