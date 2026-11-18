"""The ORM sessions the library's mapped classes are read and written through.

The tests marked ``postgres`` need a disposable PostgreSQL server (``conftest``).
"""

from __future__ import annotations

import pytest
from conftest import simulation_engine
from policyengine_simulation_persistence import (
    bounded_session,
    create_session_factory,
)
from sqlalchemy import Integer, String, create_engine, func, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.orm.exc import DetachedInstanceError


class _Base(DeclarativeBase):
    """The base of this module's own table."""


class _Written(_Base):
    """A row these tests write, in a table of their own."""

    __tablename__ = "written"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str | None] = mapped_column(String)


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


def test_an_attribute_set_to_a_sql_expression_needs_a_refresh_to_stay_readable() -> (
    None
):
    """Only the database knows the value, so it must be read before the close."""
    engine = create_engine("sqlite://")
    try:
        _Base.metadata.create_all(engine)
        session_factory = create_session_factory(engine)

        with session_factory.begin() as session:
            refreshed_row = _Written(id=1)
            unrefreshed_row = _Written(id=2)
            session.add_all([refreshed_row, unrefreshed_row])
            session.flush()
            refreshed_row.label = func.lower("LABEL")
            unrefreshed_row.label = func.lower("LABEL")
            session.flush()
            session.refresh(refreshed_row)

        assert refreshed_row.label == "label"
        with pytest.raises(DetachedInstanceError):
            _ = unrefreshed_row.label
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_a_bounded_session_limits_its_own_statements_and_commits(
    disposable_database,
) -> None:
    """The limit is set for the transaction alone, so a pooled connection keeps none."""
    engine = simulation_engine(disposable_database.superuser_url)
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
    engine = simulation_engine(disposable_database.superuser_url)
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


def _add_without_flushing(session_factory) -> None:
    with bounded_session(session_factory, statement_timeout_milliseconds=50) as session:
        session.add(_Written(id=1))


@pytest.mark.postgres
def test_a_bounded_session_limits_the_flush_at_its_commit(
    disposable_database,
) -> None:
    """What the block leaves pending is written under the same limit."""
    engine = simulation_engine(disposable_database.superuser_url)
    session_factory = create_session_factory(engine)
    try:
        _Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE FUNCTION slow_insert() RETURNS trigger "
                    "LANGUAGE plpgsql AS "
                    "$$ BEGIN PERFORM pg_sleep(5); RETURN NEW; END $$"
                )
            )
            connection.execute(
                text(
                    "CREATE TRIGGER slow_insert BEFORE INSERT ON written "
                    "FOR EACH ROW EXECUTE FUNCTION slow_insert()"
                )
            )
        with pytest.raises(OperationalError, match="statement timeout"):
            _add_without_flushing(session_factory)
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
    engine = simulation_engine(disposable_database.superuser_url)
    session_factory = create_session_factory(engine)
    try:
        with pytest.raises(OperationalError, match="statement timeout"):
            _sleep_past_the_limit(session_factory)
    finally:
        engine.dispose()
