"""Database connections for the simulation service's own role."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

if TYPE_CHECKING:
    from sqlalchemy import Engine

CONNECT_TIMEOUT_SECONDS = 5
# Each Cloud Run instance serves up to 80 concurrent requests. Each database
# operation holds a connection for one short transaction, so a small pool is
# enough.
POOL_SIZE = 2
MAX_OVERFLOW = 3


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
