"""The simulation service's connection to the Public API's database."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import TYPE_CHECKING

from policyengine_simulation_persistence import (
    create_simulation_engine,
    read_missing_table_privileges,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

# The readiness check stops waiting after three seconds; the database ends the
# query sooner, once the connection answers.
READINESS_STATEMENT_TIMEOUT_MILLISECONDS = 2000
READINESS_THREAD_NAME_PREFIX = "simulation-database-readiness"


class SimulationDatabase:
    """Run short, blocking database operations off the event loop."""

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, which connects as the simulation service's role."""
        self.engine = engine
        # One thread of its own: waiting for a pooled connection, connecting
        # and a dead socket aren't bounded by the database's time limit, so a
        # stalled database may hold this thread, but never more than this one,
        # and never one of the default executor the other routes use.
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=READINESS_THREAD_NAME_PREFIX
        )

    @classmethod
    def from_url(cls, database_url: str) -> SimulationDatabase:
        """Build the runtime engine; no connection opens until first use.

        Raise ValueError, without echoing the URL, when it is malformed.
        """
        return cls(create_simulation_engine(database_url))

    async def missing_privileges(self) -> list[str]:
        """Return each table privilege the service needs and its role lacks."""
        return await asyncio.get_running_loop().run_in_executor(
            self.executor,
            partial(
                read_missing_table_privileges,
                self.engine,
                statement_timeout_milliseconds=READINESS_STATEMENT_TIMEOUT_MILLISECONDS,
            ),
        )

    def dispose(self) -> None:
        """Stop the check's thread and close every pooled connection.

        The thread doesn't restart, so a disposed database reports every later
        check as failed; the app disposes of it once, at shutdown.
        """
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.engine.dispose()
