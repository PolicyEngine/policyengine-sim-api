"""The ORM sessions the library's mapped classes are read and written through.

The tests marked ``postgres`` need a disposable PostgreSQL server (``conftest``).
"""

from __future__ import annotations

import pytest
from policyengine_simulation_persistence import (
    bounded_session,
    create_session_factory,
    create_simulation_engine,
)
from sqlalchemy import Integer, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class _Base(DeclarativeBase):
    """The base of this module's own table."""


class _Written(_Base):
    """A row these tests write, in a table of their own."""

    __tablename__ = "written"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)


def _statement_timeout(session: Session) -> str:
    return session.execute(
        text("SELECT current_setting('statement_timeout')")
    ).scalar_one()


def test_the_session_factory_opens_sessions_on_the_engine_it_is_given() -> None:
    engine = create_engine("sqlite://")
    try:
        session_factory = create_session_factory(engine)

        with session_factory() as session:
            assert isinstance(session, Session)
            assert session.get_bind() is engine
    finally:
        engine.dispose()


def test_a_committed_object_stays_readable_after_its_session_closes() -> None:
    """Nothing is expired on commit, so a closed session's rows can be returned."""
    engine = create_engine("sqlite://")
    try:
        _Base.metadata.create_all(engine)
        session_factory = create_session_factory(engine)

        with session_factory.begin() as session:
            row = _Written(id=1)
            session.add(row)

        assert row.id == 1
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_a_bounded_session_limits_its_own_statements_and_commits(
    disposable_database,
) -> None:
    """The limit is set for the transaction alone, so a pooled connection keeps none."""
    engine = create_simulation_engine(disposable_database.superuser_url)
    session_factory = create_session_factory(engine)
    try:
        _Base.metadata.create_all(engine)
        with bounded_session(
            session_factory, statement_timeout_milliseconds=1234
        ) as session:
            limit = _statement_timeout(session)
            session.add(_Written(id=1))
        with session_factory() as session:
            later_limit = _statement_timeout(session)
            stored_row = session.get(_Written, 1)
    finally:
        engine.dispose()

    assert limit == "1234ms"
    assert later_limit == "0"
    assert stored_row is not None


def _add_then_fail(session_factory) -> None:
    with bounded_session(
        session_factory, statement_timeout_milliseconds=1000
    ) as session:
        session.add(_Written(id=1))
        session.flush()
        msg = "block failed"
        raise RuntimeError(msg)


@pytest.mark.postgres
def test_a_bounded_session_rolls_back_when_its_block_raises(
    disposable_database,
) -> None:
    engine = create_simulation_engine(disposable_database.superuser_url)
    session_factory = create_session_factory(engine)
    try:
        _Base.metadata.create_all(engine)
        with pytest.raises(RuntimeError, match="block failed"):
            _add_then_fail(session_factory)
        with session_factory() as session:
            stored_row = session.get(_Written, 1)
    finally:
        engine.dispose()

    assert stored_row is None


def _sleep_past_the_limit(session_factory) -> None:
    with bounded_session(session_factory, statement_timeout_milliseconds=50) as session:
        session.execute(text("SELECT pg_sleep(5)"))


@pytest.mark.postgres
def test_a_bounded_session_cancels_a_statement_past_its_limit(
    disposable_database,
) -> None:
    engine = create_simulation_engine(disposable_database.superuser_url)
    session_factory = create_session_factory(engine)
    try:
        with pytest.raises(OperationalError, match="statement timeout"):
            _sleep_past_the_limit(session_factory)
    finally:
        engine.dispose()
