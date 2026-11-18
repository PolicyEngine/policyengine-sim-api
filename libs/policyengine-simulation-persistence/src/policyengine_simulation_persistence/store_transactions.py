"""How the service's stores open sessions and tell a lost commit apart.

Each store operation runs in an ORM session of its own, whose statements the
database bounds. A create's statements have all run by the time its function
returns, so a database error after that is the commit's: PostgreSQL may have
applied it before the connection failed, and the rows may exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_persistence.engine import bounded_session

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from sqlalchemy.orm import Session, sessionmaker

type TransactionFactory = Callable[[], AbstractContextManager[Session]]


class EconomyRecordOutcomeUnknownError(RuntimeError):
    """A create's statements ran, but its commit failed: the rows may exist.

    PostgreSQL may have applied the commit before the connection failed, so
    the caller must not answer that nothing was created.
    """


def bounded_transactions(
    session_factory: sessionmaker[Session], *, statement_timeout_milliseconds: int
) -> TransactionFactory:
    """Return a factory of sessions in transactions bounding each statement."""

    def transaction() -> AbstractContextManager[Session]:
        return bounded_session(
            session_factory,
            statement_timeout_milliseconds=statement_timeout_milliseconds,
        )

    return transaction


def create_in_transaction[Created](
    transaction: TransactionFactory,
    insert_rows: Callable[[Session], Created],
    *,
    commit_failure: str,
) -> Created:
    """Run ``insert_rows`` in a transaction, telling a failed commit apart.

    Return what it returns. It sends every row to the database before it
    returns, so the commit has nothing to flush. An error from it leaves
    nothing written and propagates. A database error once it has run is the
    commit's: EconomyRecordOutcomeUnknownError, with ``commit_failure`` as its
    message, caused by that error.
    """
    inserted = False
    try:
        with transaction() as session:
            created = insert_rows(session)
            inserted = True
    except SQLAlchemyError as error:
        if not inserted:
            raise
        raise EconomyRecordOutcomeUnknownError(commit_failure) from error
    return created
