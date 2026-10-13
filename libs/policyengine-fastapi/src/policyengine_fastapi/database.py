"""SQLModel engine and per-request session dependency helpers."""

from collections.abc import Callable, Generator
from typing import Any

from sqlalchemy import Engine, StaticPool, create_engine
from sqlmodel import Session

# Generally following the guidance for SQL in fastAPI here
# https://fastapi.tiangolo.com/tutorial/sql-databases/

# NOTE: if you don't use StaticPool with in-memory sqlite you will get
# errors.
# https://sqlmodel.tiangolo.com/tutorial/fastapi/tests/#configure-the-in-memory-database


def create_sqlite_engine(filename: str | None = None) -> Engine:
    """Create a SQLite engine for desktop use and tests.

    Without ``filename`` the database is in memory; StaticPool keeps that single
    in-memory connection shared across threads.
    """
    sqlite_url = f"sqlite:///{filename}" if filename else "sqlite:///:memory:"
    connect_args = {"check_same_thread": False}
    return create_engine(sqlite_url, connect_args=connect_args, poolclass=StaticPool)


SessionGeneratorFactory = Callable[[], Generator[Session, Any, None]]


def create_session_dep(engine: Engine) -> SessionGeneratorFactory:
    """Return a FastAPI dependency that yields one SQLModel session per request.

    The session is bound to ``engine`` and closed when the request finishes.
    """

    def session_dep() -> Generator[Session]:
        with Session(engine) as session:
            yield session

    return session_dep
