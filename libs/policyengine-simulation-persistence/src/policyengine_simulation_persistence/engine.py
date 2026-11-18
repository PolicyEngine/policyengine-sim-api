"""Database connections for the simulation service and the v2 Modal calls."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection, Engine, Select
    from sqlalchemy.orm import Session

CONNECT_TIMEOUT_SECONDS = 5
# Each Cloud Run instance serves up to 80 concurrent requests. Each database
# operation holds a connection for one short transaction, so a small pool is
# enough.
POOL_SIZE = 2
MAX_OVERFLOW = 3
# A v2 Modal call connects across the internet to the database's pooler.
UNPOOLED_CONNECT_TIMEOUT_SECONDS = 10


def normalized_database_url(database_url: str) -> URL:
    """Return ``database_url`` with the psycopg 3 driver selected.

    The stored secrets use the canonical ``postgresql://`` scheme. Raise
    ValueError, without echoing the value, when the URL is missing, malformed
    or incomplete.
    """
    if not database_url or database_url != database_url.strip():
        msg = "Simulation database URL is missing or malformed"
        raise ValueError(msg)
    try:
        parsed = make_url(database_url)
    except (ArgumentError, ValueError):
        # Raised without the parser's message, which may quote the URL.
        msg = "Simulation database URL is malformed"
        raise ValueError(msg) from None
    if parsed.drivername != "postgresql":
        msg = "Simulation database URL must use postgresql://"
        raise ValueError(msg)
    if not parsed.username or not parsed.host or not parsed.database:
        msg = "Simulation database URL is incomplete"
        raise ValueError(msg)
    return parsed.set(drivername="postgresql+psycopg")


def create_simulation_engine(database_url: str) -> Engine:
    """Build the engine the simulation service uses; it creates no objects."""
    return create_engine(
        normalized_database_url(database_url),
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            # Never use server-side prepared statements, so the same engine
            # works through Supabase's session and transaction poolers.
            "prepare_threshold": None,
        },
    )


def create_unpooled_engine(database_url: str) -> Engine:
    """Build an engine that opens a connection per operation and closes it after.

    The v2 Modal coordinators, workers and household functions, and the
    comparison path, spend most of their lifetime calculating or waiting, so
    each database session ends with its short operation instead of holding a
    pooler slot through that idle time. It creates no objects.
    """
    return create_engine(
        normalized_database_url(database_url),
        poolclass=NullPool,
        connect_args={"connect_timeout": UNPOOLED_CONNECT_TIMEOUT_SECONDS},
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return the factory of ORM sessions on ``engine``.

    Attributes keep their values after the transaction commits, so a function
    can return what it wrote once its session has closed. An attribute set to a
    SQL expression, such as ``func.now()``, is the exception: only the database
    knows its value, so refresh the object before the session closes. Run a
    transaction as ``with session_factory.begin() as session:``.
    """
    return sessionmaker(engine, expire_on_commit=False)


def _set_statement_timeout(statement_timeout_milliseconds: int) -> Select[tuple[str]]:
    """Return the statement that bounds the rest of the current transaction."""
    return select(
        func.set_config(
            "statement_timeout",
            str(statement_timeout_milliseconds),
            True,  # noqa: FBT003  # set_config's is_local argument
        )
    )


@contextmanager
def bounded_transaction(
    engine: Engine, *, statement_timeout_milliseconds: int
) -> Iterator[Connection]:
    """Yield a connection in a transaction whose statements the database bounds.

    The transaction commits when the block ends and rolls back when it
    raises. ``statement_timeout`` is set with ``set_config`` local to the
    transaction, so it works through Supabase's transaction pooler and never
    stays on the pooled connection; a statement that runs longer raises the
    database's cancellation as an OperationalError.
    """
    with engine.begin() as connection:
        connection.execute(_set_statement_timeout(statement_timeout_milliseconds))
        yield connection


@contextmanager
def bounded_session(
    session_factory: sessionmaker[Session], *, statement_timeout_milliseconds: int
) -> Iterator[Session]:
    """Yield a session in a transaction whose statements the database bounds.

    It is ``bounded_transaction`` for code on the ORM: the transaction commits
    when the block ends, flushing what the session holds under the same limit,
    and rolls back when it raises. ``statement_timeout`` is local to the
    transaction, so it works through Supabase's transaction pooler and never
    stays on the pooled connection; a statement that runs longer raises the
    database's cancellation as an OperationalError.
    """
    with session_factory.begin() as session:
        session.execute(_set_statement_timeout(statement_timeout_milliseconds))
        yield session
