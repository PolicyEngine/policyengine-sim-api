"""What tests of ORM sessions watch, and the real failures they cause.

A session runs its statements through events a test can listen to, and a store's
create means different things depending on where it fails: at the commit the
rows may exist, before it nothing was written. These helpers record what a
session or connection does, and end a session's connection at a chosen moment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import event, func, select

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine
    from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker

# How long PostgreSQL may take to end a backend it is told to end.
TERMINATE_TIMEOUT_MILLISECONDS = 5_000


def terminate_backend(engine: Engine, backend_id: int) -> None:
    """End the backend from another connection, and wait until it has gone."""
    with engine.connect() as connection:
        ended = connection.execute(
            select(
                func.pg_terminate_backend(backend_id, TERMINATE_TIMEOUT_MILLISECONDS)
            )
        ).scalar_one()
    assert ended


def end_connection_at(
    session_factory: sessionmaker[Session], session_event: str, engine: Engine
) -> list[int]:
    """End each session's connection at ``session_event``, from another one.

    Return a list that gains, each time, how many added rows the session had
    not yet flushed. A savepoint's release fires the session's commit events
    too, so only the outermost transaction's count.
    """
    unflushed_counts: list[int] = []

    def end_the_connection(session: Session, *_event_details: object) -> None:
        if session.in_nested_transaction():
            return
        unflushed_counts.append(len(session.new))
        backend_id = session.execute(select(func.pg_backend_pid())).scalar_one()
        terminate_backend(engine, backend_id)

    event.listen(session_factory, session_event, end_the_connection)
    return unflushed_counts


def end_connection_before_statement(engine: Engine, statement_start: str) -> list[int]:
    """End the connection about to send the first statement starting so.

    Return a list that gains the ended backend's ID.
    """
    ended_backends: list[int] = []

    def end_the_connection(
        _connection, cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if ended_backends or not statement.startswith(statement_start):
            return
        ended_backends.append(cursor.connection.info.backend_pid)
        terminate_backend(engine, ended_backends[0])

    event.listen(engine, "before_cursor_execute", end_the_connection)
    return ended_backends


def statements_sent(engine_or_connection: Engine | Connection) -> list[str]:
    """Return a list that gains each statement sent to the database from now on."""
    statements: list[str] = []

    def record_statement(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        statements.append(statement)

    event.listen(engine_or_connection, "before_cursor_execute", record_statement)
    return statements


def orm_statement_flags(session: Session) -> list[bool]:
    """Return a list that gains, per statement, whether it names mapped classes."""
    flags: list[bool] = []
    event.listen(
        session,
        "do_orm_execute",
        lambda execute_state: flags.append(execute_state.is_orm_statement),
    )
    return flags


def statements_naming_no_mapped_class(
    session_or_factory: Session | sessionmaker[Session],
) -> list[str]:
    """Return a list that gains each statement run without a mapped class."""
    statements: list[str] = []

    def record_core_statement(execute_state: ORMExecuteState) -> None:
        if not execute_state.is_orm_statement:
            statements.append(str(execute_state.statement))

    event.listen(session_or_factory, "do_orm_execute", record_core_statement)
    return statements
