"""ORM sessions on the disposable Postgres, and a record of what they send.

The stores run every statement on a ``Session``. ``StatementLog`` counts what
each session executes and what reaches the database, so a store still using a
Core connection, whose statements no session sees, fails the test using it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker

CONNECTION_REFUSED_MESSAGE = "(EMAXCONNSESSION) max clients reached in session mode"
# The role the comparison path's tables grant to, which a test connection
# takes on with SET ROLE: it has no login in the disposable database.
RUNTIME_ROLE = "policyengine_v2_runtime"


class StatementLog:
    """What a session factory's sessions executed and sent to the database."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        """Start recording ``session_factory``'s sessions and their engine."""
        self.session_statements = 0
        self.sent: list[str] = []
        self.commits = 0
        engine = session_factory.kw["bind"]
        event.listen(session_factory, "do_orm_execute", self._count_session_statement)
        event.listen(engine, "before_cursor_execute", self._record_sent)
        event.listen(engine, "commit", self._count_commit)

    def _count_session_statement(self, _state: ORMExecuteState) -> None:
        self.session_statements += 1

    def _record_sent(
        self, _connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        self.sent.append(" ".join(statement.split()))

    def _count_commit(self, _connection) -> None:
        self.commits += 1

    def starting_with(self, prefix: str) -> list[str]:
        """Return the statements sent that begin with ``prefix``."""
        return [statement for statement in self.sent if statement.startswith(prefix)]

    def assert_every_statement_ran_on_a_session(self) -> None:
        """Fail if anything reached the database without passing through a session."""
        assert self.sent, "the store sent no statement"
        # A session statement that could not connect reached no database, so
        # sessions may have run more statements than arrived, never fewer.
        assert len(self.sent) <= self.session_statements, self.sent


class RetryingSessionOpener:
    """Open a session, and open another after one refused connection.

    It records each operation's name, each refusal and, given a log, what
    reached the database while the session opened.
    """

    def __init__(self, statements: StatementLog | None = None) -> None:
        """Report what ``statements``, if given, records during each opening."""
        self.statements = statements
        self.operation_names: list[str] = []
        self.refusals: list[OperationalError] = []
        self.sent_while_opening: list[list[str]] = []

    def __call__(
        self, open_session: Callable[[], Session], *, operation_name: str
    ) -> Session:
        """Return ``open_session``'s session, retrying one refused connection."""
        self.operation_names.append(operation_name)
        already_sent = len(self.statements.sent) if self.statements else 0
        try:
            session = open_session()
        except OperationalError as refusal:
            self.refusals.append(refusal)
            session = open_session()
        if self.statements:
            self.sent_while_opening.append(self.statements.sent[already_sent:])
        return session


def refuse_connection_numbers(
    session_factory: sessionmaker[Session], numbers: Collection[int]
) -> list[Any]:
    """Refuse the factory's attempts to connect that have these 1-based numbers.

    Returns a list that gains one entry per attempt to connect.
    """
    attempts: list[Any] = []

    def refuse(dialect, _record, _arguments, _keywords) -> None:
        attempts.append(object())
        if len(attempts) in numbers:
            raise dialect.loaded_dbapi.OperationalError(CONNECTION_REFUSED_MESSAGE)

    event.listen(session_factory.kw["bind"], "do_connect", refuse)
    return attempts


def refuse_connections(session_factory: sessionmaker[Session], count: int) -> list[Any]:
    """Refuse the factory's first ``count`` connections, as a full pooler does.

    Returns a list that gains one entry per attempt to connect.
    """
    return refuse_connection_numbers(session_factory, range(1, count + 1))


def limit_open_connections(session_factory: sessionmaker[Session], limit: int) -> None:
    """Refuse a connection while ``limit`` are open, as a full session pooler does."""
    engine = session_factory.kw["bind"]
    open_connections: list[Any] = []

    def refuse_over_the_limit(dialect, _record, _arguments, _keywords) -> None:
        if len(open_connections) >= limit:
            raise dialect.loaded_dbapi.OperationalError(CONNECTION_REFUSED_MESSAGE)

    def record_opened(dbapi_connection, _connection_record) -> None:
        open_connections.append(dbapi_connection)

    def record_closed(dbapi_connection, _connection_record) -> None:
        open_connections.remove(dbapi_connection)

    event.listen(engine, "do_connect", refuse_over_the_limit)
    event.listen(engine, "connect", record_opened)
    event.listen(engine, "close", record_closed)


def act_as_role(engine: Any, role: str) -> None:
    """Make every connection of ``engine`` act as ``role``."""

    def set_role(dbapi_connection, _connection_record) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f"SET ROLE {role}")

    event.listen(engine, "connect", set_role)
