"""Transactions that fail at a chosen point, for the stores' create tests.

A store's create tells a commit that failed after its rows were flushed, so
the rows may exist, from a flush that failed, so nothing was written. These
fakes stand in for the store's transaction factory to make each failure
happen without a database.
"""

from __future__ import annotations

from typing import Self

from sqlalchemy.exc import OperationalError


class RecordingSession:
    """A session that keeps what it is given and runs nothing."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.statements: list[object] = []
        self.flushed = False

    def add(self, instance: object) -> None:
        self.added.append(instance)

    def flush(self) -> None:
        self.flushed = True

    def execute(self, statement: object) -> None:
        self.statements.append(statement)


class CommitFailingTransaction:
    """A transaction whose block runs, and whose commit then fails."""

    def __init__(self, session: RecordingSession) -> None:
        self.session = session

    def __enter__(self) -> RecordingSession:
        return self.session

    def __exit__(self, error_type, error, traceback) -> None:
        if error is None:
            statement = "COMMIT"
            raise OperationalError(statement, {}, Exception("connection lost"))


class FlushFailingTransaction:
    """A transaction whose flush fails, so nothing is committed."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, error_type, error, traceback) -> None:
        return None

    def add(self, instance: object) -> None:
        return None

    def flush(self) -> None:
        failed_statement = "INSERT"
        raise OperationalError(failed_statement, {}, Exception("connection lost"))
