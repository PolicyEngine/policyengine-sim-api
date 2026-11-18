"""How a store runs a create, telling a lost commit from a refused write."""

from __future__ import annotations

import pytest
from policyengine_simulation_persistence import EconomyRecordOutcomeUnknownError
from policyengine_simulation_persistence.store_transactions import (
    create_in_transaction,
)
from sqlalchemy.exc import OperationalError
from transaction_fakes import CommitFailingTransaction, RecordingSession

COMMIT_FAILURE = "the commit failed after the rows were inserted"


class _OpenTransaction:
    """A transaction whose commit succeeds."""

    def __init__(self, session: RecordingSession) -> None:
        self.session = session

    def __enter__(self) -> RecordingSession:
        return self.session

    def __exit__(self, error_type, error, traceback) -> None:
        return None


def test_a_create_returns_what_its_insert_returns() -> None:
    session = RecordingSession()

    created = create_in_transaction(
        lambda: _OpenTransaction(session),
        lambda given_session: given_session is session,
        commit_failure=COMMIT_FAILURE,
    )

    assert created is True


def test_a_failed_commit_after_the_insert_leaves_the_outcome_unknown() -> None:
    session = RecordingSession()

    with pytest.raises(
        EconomyRecordOutcomeUnknownError, match=COMMIT_FAILURE
    ) as raised:
        create_in_transaction(
            lambda: CommitFailingTransaction(session),
            lambda _session: "created",
            commit_failure=COMMIT_FAILURE,
        )

    assert isinstance(raised.value.__cause__, OperationalError)


def test_a_database_error_from_the_insert_is_raised_as_it_is() -> None:
    refused = OperationalError("INSERT", {}, Exception("connection lost"))

    def refuse(_session: RecordingSession) -> None:
        raise refused

    with pytest.raises(OperationalError) as raised:
        create_in_transaction(
            lambda: _OpenTransaction(RecordingSession()),
            refuse,
            commit_failure=COMMIT_FAILURE,
        )

    assert raised.value is refused


def test_any_other_error_is_never_taken_for_a_lost_commit() -> None:
    def refuse(_session: RecordingSession) -> None:
        raise LookupError

    with pytest.raises(LookupError):
        create_in_transaction(
            lambda: _OpenTransaction(RecordingSession()),
            refuse,
            commit_failure=COMMIT_FAILURE,
        )
