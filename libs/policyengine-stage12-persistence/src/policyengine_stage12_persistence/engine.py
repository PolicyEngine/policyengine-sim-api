"""The ORM sessions the Stage 12 stores run their operations on.

Each operation of a store runs in one ``Session`` of its own. Opening that
session sends one harmless statement, ``CONNECTION_CHECK``, so the session
checks out its connection and any failure to connect happens there, before the
operation has sent anything. A ``SessionOpener`` wraps only that step, so it
may repeat it without any risk of repeating a write.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final, Protocol

# Every Stage 12 store connects through the simulation service library's
# unpooled sessions, which check the URL with that library's one URL check.
from policyengine_simulation_persistence.engine import (
    create_single_connection_session_factory,
    create_unpooled_session_factory,
)
from sqlalchemy import inspect, select, true

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from sqlalchemy.engine.reflection import Inspector
    from sqlalchemy.orm import Session, sessionmaker

CONNECTION_CHECK: Final = select(true())
# The stores read rows as plain values and keep no mapped object in a session,
# so an UPDATE or DELETE has no object to bring up to date. Without this the
# session would add the primary key to the statement's RETURNING to find some.
WITHOUT_OBJECT_SYNC: Final[Mapping[str, Any]] = {"synchronize_session": False}


class SessionOpener(Protocol):
    """Open the session that one store operation runs on.

    ``open_session`` only connects: it sends ``CONNECTION_CHECK`` and no
    statement of the operation. The operation's statements, and the COMMIT or
    ROLLBACK that ends it, run after the opener returns. An opener may therefore
    call ``open_session`` again after a failure without risking a duplicate
    write, and never sees a later failure.
    """

    def __call__(
        self,
        open_session: Callable[[], Session],
        *,
        operation_name: str,
    ) -> Session:
        """Return the session ``open_session`` opens for ``operation_name``."""
        ...


def open_session_once(
    open_session: Callable[[], Session],
    *,
    operation_name: str,  # noqa: ARG001  # the SessionOpener protocol passes it by keyword
) -> Session:
    """Open a session once, so a refused connection fails the operation."""
    return open_session()


def create_stage12_session_factory(database_url: str) -> sessionmaker[Session]:
    """Return a factory of sessions that each open their own connection.

    Raises ValueError when the URL is invalid. Nothing connects until a session
    runs its first statement.
    """
    return create_unpooled_session_factory(database_url)


def create_stage12_check_session_factory(
    database_url: str,
) -> sessionmaker[Session]:
    """Return the session factory of the deployment check.

    Its sessions and schema reads take turns on one connection, so the check
    needs one free client slot of the database's pooler. Raises ValueError when
    the URL is invalid. Close the connection with ``dispose_session_factory``.
    """
    return create_single_connection_session_factory(database_url)


def inspect_live_schema(session_factory: sessionmaker[Session]) -> Inspector:
    """Return an inspector of the database ``session_factory`` connects to.

    It takes a connection from the factory's engine at once, and again for each
    thing it reads. Without a pool, each of those is a new connection.
    """
    return inspect(session_factory.kw["bind"])


class StoreSessions:
    """Opens the session of each operation of one store."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        open_session: SessionOpener = open_session_once,
    ) -> None:
        """Open sessions of ``session_factory`` through ``open_session``."""
        self._session_factory = session_factory
        self._open_session = open_session

    @classmethod
    def for_store(
        cls,
        database_url: str | None,
        *,
        session_factory: sessionmaker[Session] | None,
        open_session: SessionOpener,
        missing_url_message: str,
    ) -> StoreSessions:
        """Use ``session_factory`` if given, else build one from ``database_url``.

        Raises ValueError with ``missing_url_message`` when neither is given,
        and ValueError when the URL is invalid.
        """
        if session_factory is None:
            if database_url is None:
                raise ValueError(missing_url_message)
            session_factory = create_stage12_session_factory(database_url)
        return cls(session_factory, open_session)

    def _connected_session(self) -> Session:
        session = self._session_factory()
        try:
            session.execute(CONNECTION_CHECK).scalar_one()
        except BaseException:
            session.close()
            raise
        return session

    @contextmanager
    def reading(self, operation_name: str) -> Iterator[Session]:
        """Yield a connected session; closing it ends what it did, uncommitted."""
        # Opening is the only step the opener sees. Every statement of the
        # operation, its COMMIT and its ROLLBACK run after the opener returns,
        # so an opener that retries can never repeat what the database received.
        session = self._open_session(
            self._connected_session,
            operation_name=operation_name,
        )
        with session:
            yield session

    @contextmanager
    def transaction(self, operation_name: str) -> Iterator[Session]:
        """Yield a connected session and commit when the block ends.

        An exception from the block rolls everything back, and so does a
        COMMIT that fails; neither is retried.
        """
        with self.reading(operation_name) as session:
            yield session
            session.commit()
