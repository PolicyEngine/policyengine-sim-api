"""Connection settings for the simulation service's database.

The tests marked ``postgres`` need a disposable PostgreSQL server (``conftest``).
"""

from __future__ import annotations

import sqlite3

import pytest
from policyengine_simulation_persistence import (
    bounded_transaction,
    create_simulation_engine,
    create_unpooled_engine,
    normalized_database_url,
)
from policyengine_simulation_persistence import engine as engine_module
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool, QueuePool


def test_database_url_selects_psycopg_without_changing_the_secret_format() -> None:
    url = normalized_database_url(
        "postgresql://simulation_service:secret@db.example:5432/postgres"
    )

    assert url.drivername == "postgresql+psycopg"
    assert url.username == "simulation_service"
    assert url.host == "db.example"
    assert url.database == "postgres"


@pytest.mark.parametrize(
    "database_url",
    [
        "",
        " postgresql://user:secret@db.example/postgres",
        "postgres://user:secret@db.example/postgres",
        "postgresql+psycopg://user:secret@db.example/postgres",
        "postgresql://db.example/postgres",
        "postgresql://user:secret@db.example",
        "not a url",
    ],
)
def test_malformed_database_urls_are_rejected_without_echoing_them(
    database_url,
) -> None:
    with pytest.raises(ValueError, match="Simulation database URL") as error:
        normalized_database_url(database_url)

    assert "secret" not in str(error.value)
    assert error.value.__cause__ is None or "secret" not in str(error.value.__cause__)


def test_runtime_engine_never_prepares_server_side_statements(monkeypatch) -> None:
    captured_options = {}

    def capture_engine(url, **options):
        captured_options.update(options, url=url)
        return object()

    monkeypatch.setattr(
        "policyengine_simulation_persistence.engine.create_engine",
        capture_engine,
    )

    create_simulation_engine(
        "postgresql://simulation_service:secret@db.example/postgres"
    )

    assert captured_options["url"].drivername == "postgresql+psycopg"
    assert captured_options["connect_args"]["prepare_threshold"] is None
    assert captured_options["connect_args"]["connect_timeout"] > 0
    assert captured_options["pool_pre_ping"] is True
    assert captured_options["pool_size"] >= 1


def test_the_unpooled_engine_holds_no_connection_and_selects_psycopg(
    monkeypatch,
) -> None:
    captured_options = {}

    def capture_engine(url, **options):
        captured_options.update(options, url=url)
        return object()

    monkeypatch.setattr(engine_module, "create_engine", capture_engine)

    create_unpooled_engine("postgresql://modal_worker:secret@db.example/postgres")

    assert captured_options["url"].drivername == "postgresql+psycopg"
    assert captured_options["poolclass"] is NullPool
    assert captured_options["connect_args"] == {"connect_timeout": 10}


@pytest.mark.parametrize("fail_transaction", [False, True])
def test_the_unpooled_engine_closes_connections_between_transactions(
    monkeypatch, fail_transaction
) -> None:
    """A long calculation holds no idle database session after its operation."""

    def local_engine(_url, **options):
        # Exercise the unpooled configuration with a real local DBAPI.
        # PostgreSQL's default is QueuePool; SQLite's memory default differs.
        options.pop("connect_args")
        options.setdefault("poolclass", QueuePool)
        return create_engine("sqlite://", **options)

    monkeypatch.setattr(engine_module, "create_engine", local_engine)
    engine = create_unpooled_engine("postgresql://runtime:secret@db.example/postgres")
    try:
        try:
            with engine.begin() as connection:
                physical_connection = connection.connection.driver_connection
                assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
                if fail_transaction:
                    _fail_the_calculation()
        except RuntimeError:
            assert fail_transaction

        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            physical_connection.execute("SELECT 1")

        with engine.begin() as connection:
            assert connection.connection.driver_connection is not physical_connection
            assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
    finally:
        engine.dispose()


def _fail_the_calculation() -> None:
    """Raise the failure a calculation inside the transaction would."""
    msg = "calculation failed"
    raise RuntimeError(msg)


@pytest.mark.postgres
def test_a_bounded_transaction_limits_its_own_statements_and_commits(
    disposable_database,
) -> None:
    """The limit is set for the transaction alone, so a pooled connection keeps none."""
    engine = create_simulation_engine(disposable_database.superuser_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE written (id integer)"))
        with bounded_transaction(
            engine, statement_timeout_milliseconds=1234
        ) as connection:
            limit = connection.execute(
                text("SELECT current_setting('statement_timeout')")
            ).scalar_one()
            connection.execute(text("INSERT INTO written VALUES (1)"))
        with engine.connect() as connection:
            later_limit = connection.execute(
                text("SELECT current_setting('statement_timeout')")
            ).scalar_one()
            count = connection.execute(
                text("SELECT count(*) FROM written")
            ).scalar_one()
    finally:
        engine.dispose()

    assert limit == "1234ms"
    assert later_limit == "0"
    assert count == 1


def _insert_then_fail(engine) -> None:
    with bounded_transaction(engine, statement_timeout_milliseconds=1000) as connection:
        connection.execute(text("INSERT INTO written VALUES (1)"))
        msg = "block failed"
        raise RuntimeError(msg)


@pytest.mark.postgres
def test_a_bounded_transaction_rolls_back_when_its_block_raises(
    disposable_database,
) -> None:
    engine = create_simulation_engine(disposable_database.superuser_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE written (id integer)"))
        with pytest.raises(RuntimeError, match="block failed"):
            _insert_then_fail(engine)
        with engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM written")
            ).scalar_one()
    finally:
        engine.dispose()

    assert count == 0
