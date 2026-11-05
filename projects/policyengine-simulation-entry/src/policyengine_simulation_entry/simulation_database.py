"""The simulation service's connection to the Public API's database."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import TYPE_CHECKING

from policyengine_observability import TelemetryLimits
from policyengine_simulation_persistence import (
    create_simulation_engine,
    read_missing_table_privileges,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

# The readiness check stops waiting after three seconds; the database ends
# each statement of the check that runs longer than this, once connected.
READINESS_STATEMENT_TIMEOUT_MILLISECONDS = 2000
READINESS_THREAD_NAME_PREFIX = "simulation-database-readiness"

# The readiness route is public, so it only says whether the check failed or a
# privilege is missing; these log messages say which. The observability
# runtime's log handler keeps a record's message but not its ``extra`` fields,
# so everything an operator needs is in the message.
UNREACHABLE_LOG_EVENT = "simulation_database_unreachable"
MISSING_PRIVILEGES_LOG_EVENT = "simulation_database_missing_privileges"
# The runtime cuts a longer message; a long list is logged in several.
LOG_MESSAGE_MAX_CHARACTERS = TelemetryLimits().max_string_length


def missing_privilege_log_messages(missing_privileges: Sequence[str]) -> list[str]:
    """Name every missing privilege, in order, in messages kept whole.

    Each message is ``"simulation_database_missing_privileges: <name>, …"``
    and no longer than ``LOG_MESSAGE_MAX_CHARACTERS``.
    """
    prefix = f"{MISSING_PRIVILEGES_LOG_EVENT}: "
    messages: list[str] = []
    names_in_message: list[str] = []
    for name in missing_privileges:
        longer = prefix + ", ".join([*names_in_message, name])
        if names_in_message and len(longer) > LOG_MESSAGE_MAX_CHARACTERS:
            messages.append(prefix + ", ".join(names_in_message))
            names_in_message = [name]
        else:
            names_in_message.append(name)
    if names_in_message:
        messages.append(prefix + ", ".join(names_in_message))
    return messages


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
