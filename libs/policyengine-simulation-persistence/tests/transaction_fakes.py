"""Transactions that fail at a chosen point, for the stores' create tests.

A store's create tells a commit that failed after its rows were inserted,
so the rows may exist, from a statement that failed, so nothing was
written. These fakes stand in for the store's transaction factory to make
each failure happen without a database.
"""

from __future__ import annotations

from typing import Self

from sqlalchemy.exc import OperationalError


class RecordingConnection:
    """A connection that records each statement and runs none."""

    def __init__(self) -> None:
        self.statements: list[object] = []

    def execute(self, statement, *arguments):
        self.statements.append(statement)


class CommitFailingTransaction:
    """A transaction whose block runs, and whose commit then fails."""

    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection

    def __enter__(self) -> RecordingConnection:
        return self.connection

    def __exit__(self, error_type, error, traceback) -> None:
        if error is None:
            statement = "COMMIT"
            raise OperationalError(statement, {}, Exception("connection lost"))


class StatementFailingTransaction:
    """A transaction whose first statement fails, so nothing is committed."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, error_type, error, traceback) -> None:
        return None

    def execute(self, statement, *arguments):
        failed_statement = "INSERT"
        raise OperationalError(failed_statement, {}, Exception("connection lost"))
