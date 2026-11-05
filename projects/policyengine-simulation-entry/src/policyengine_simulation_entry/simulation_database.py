"""The simulation service's connection to the Public API's database."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import TYPE_CHECKING

from policyengine_simulation_persistence import (
    create_simulation_engine,
    read_missing_table_privileges,
)

from policyengine_simulation_entry.schemas import SimulationDatabaseReadiness

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy import Engine

    from policyengine_simulation_entry.entry_runtime import (
        SimulationDatabaseConnection,
    )

logger = logging.getLogger(__name__)

# The readiness check stops waiting after three seconds; the database ends
# each statement of the check that runs longer than this, once connected.
READINESS_STATEMENT_TIMEOUT_MILLISECONDS = 2000
READINESS_THREAD_NAME_PREFIX = "simulation-database-readiness"
# How long a check that reached the database answers /ready, so repeated
# requests to the public route don't each take a pooled connection.
READINESS_CACHE_SECONDS = 30.0

# The readiness route is public, so it only says whether the check failed or a
# privilege is missing; these log messages say which. The observability
# runtime's log handler keeps a record's message but not its ``extra`` fields,
# so everything an operator needs is in the message.
UNREACHABLE_LOG_EVENT = "simulation_database_unreachable"
MISSING_PRIVILEGES_LOG_EVENT = "simulation_database_missing_privileges"
# PostgreSQL's five-character error code, such as 28P01 for a refused password.
_SQLSTATE = re.compile(r"[0-9A-Z]{5}")


def unreachable_log_message(error: BaseException) -> str:
    """Name a failed check's error type, and the driver's error and its code.

    Never the error's text, which can echo connection details. The SQLSTATE
    code, when the database sent one, tells a refused login (``28P01``) from a
    statement timeout (``57014``); a network failure has none.
    """
    message = f"{UNREACHABLE_LOG_EVENT}: {type(error).__name__}"
    driver_error = getattr(error, "orig", None)
    if driver_error is None:
        return message
    sqlstate = getattr(driver_error, "sqlstate", None)
    if not isinstance(sqlstate, str) or not _SQLSTATE.fullmatch(sqlstate):
        sqlstate = "none"
    return f"{message} ({type(driver_error).__name__}, SQLSTATE {sqlstate})"


def missing_privilege_log_messages(
    missing_privileges: Sequence[str], *, max_characters: int
) -> list[str]:
    """Name every missing privilege, in order, in messages kept whole.

    Each message is ``"simulation_database_missing_privileges: <name>, …"``
    and no longer than ``max_characters``, the observability runtime's limit:
    it cuts a longer message, so a long list is logged in several.
    """
    prefix = f"{MISSING_PRIVILEGES_LOG_EVENT}: "
    messages: list[str] = []
    names_in_message: list[str] = []
    for name in missing_privileges:
        longer = prefix + ", ".join([*names_in_message, name])
        if names_in_message and len(longer) > max_characters:
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

        The thread doesn't restart, so every later check of a disposed database
        fails, though ``/ready`` may still answer from a check that reached it
        before, for up to ``READINESS_CACHE_SECONDS``. The app disposes of it
        once, at shutdown.
        """
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.engine.dispose()


class SimulationDatabaseReadinessCheck:
    """Check the database for /ready, reusing a recent check.

    A check that reached the database answers every request for
    ``cache_seconds``, whether or not a privilege was missing, and logs what
    is missing once. A failed check isn't reused: the next request checks
    again, so a cold instance that reaches its database a moment later says
    so at once. Requests that arrive while a check runs share it, and a
    request that stops waiting leaves it running for the others.
    """

    def __init__(
        self,
        database: SimulationDatabaseConnection,
        *,
        timeout_seconds: float,
        log_message_max_characters: int,
        cache_seconds: float = READINESS_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Check ``database``, waiting at most ``timeout_seconds`` each time.

        ``log_message_max_characters`` is the limit of the observability
        runtime the service logs through.
        """
        self.database = database
        self.timeout_seconds = timeout_seconds
        self.log_message_max_characters = log_message_max_characters
        self.cache_seconds = cache_seconds
        self.clock = clock
        self._reusable: SimulationDatabaseReadiness | None = None
        self._reusable_until = 0.0
        self._running: asyncio.Task[SimulationDatabaseReadiness] | None = None

    async def readiness(self) -> SimulationDatabaseReadiness:
        """Return a recent check that reached the database, else check now."""
        if self._reusable is not None and self.clock() < self._reusable_until:
            return self._reusable
        if self._running is None:
            self._running = asyncio.ensure_future(self._check())
        return await asyncio.shield(self._running)

    async def _check(self) -> SimulationDatabaseReadiness:
        try:
            readiness = await self._check_once()
        finally:
            self._running = None
        if readiness.reachable:
            self._reusable = readiness
            self._reusable_until = self.clock() + self.cache_seconds
        return readiness

    async def _check_once(self) -> SimulationDatabaseReadiness:
        """Say whether the check succeeded and the role holds every privilege.

        The route is public, so the result says only yes or no; the log names
        the failure's type or each missing privilege.
        """
        try:
            missing_privileges = await asyncio.wait_for(
                self.database.missing_privileges(), timeout=self.timeout_seconds
            )
        # Any failure to reach or read the database is reported, never raised:
        # the old-gateway routes this check covers do not use it.
        except Exception as error:  # noqa: BLE001  # reported as unreachable
            logger.warning("%s", unreachable_log_message(error))
            return SimulationDatabaseReadiness(
                reachable=False, has_required_privileges=None
            )
        for message in missing_privilege_log_messages(
            missing_privileges, max_characters=self.log_message_max_characters
        ):
            logger.warning("%s", message)
        return SimulationDatabaseReadiness(
            reachable=True, has_required_privileges=not missing_privileges
        )
