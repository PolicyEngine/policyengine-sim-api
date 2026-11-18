"""Connection settings for the simulation service's database."""

from __future__ import annotations

import sqlite3

import pytest
from policyengine_simulation_persistence import (
    create_simulation_session_factory,
    create_unpooled_session_factory,
    dispose_session_factory,
    normalized_database_url,
)
from policyengine_simulation_persistence import engine as engine_module
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
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


def _capture_engine_options(monkeypatch) -> tuple[dict, object]:
    captured_options: dict = {}
    built_engine = object()

    def capture_engine(url, **options):
        captured_options.update(options, url=url)
        return built_engine

    monkeypatch.setattr(engine_module, "create_engine", capture_engine)
    return captured_options, built_engine


def test_the_service_sessions_never_prepare_server_side_statements(monkeypatch) -> None:
    captured_options, built_engine = _capture_engine_options(monkeypatch)

    session_factory = create_simulation_session_factory(
        "postgresql://simulation_service:secret@db.example/postgres"
    )

    assert session_factory.kw["bind"] is built_engine
    assert session_factory.kw["expire_on_commit"] is False
    assert captured_options["url"].drivername == "postgresql+psycopg"
    assert captured_options["connect_args"]["prepare_threshold"] is None
    assert captured_options["connect_args"]["connect_timeout"] > 0
    assert captured_options["pool_pre_ping"] is True
    assert captured_options["pool_size"] == engine_module.POOL_SIZE


def test_the_unpooled_session_factory_keeps_no_connection(monkeypatch) -> None:
    captured_options, built_engine = _capture_engine_options(monkeypatch)

    session_factory = create_unpooled_session_factory(
        "postgresql://modal_worker:secret@db.example/postgres"
    )

    assert session_factory.kw["bind"] is built_engine
    assert session_factory.kw["expire_on_commit"] is False
    assert captured_options["poolclass"] is NullPool
    assert captured_options["connect_args"] == {"connect_timeout": 10}


def test_disposing_a_session_factory_closes_its_pooled_connections() -> None:
    engine = create_engine("sqlite://", poolclass=QueuePool)
    session_factory = sessionmaker(engine)
    with session_factory() as session:
        session.execute(text("SELECT 1"))
    pool_before = engine.pool
    assert pool_before.checkedin() == 1

    dispose_session_factory(session_factory)

    assert engine.pool is not pool_before
    assert pool_before.checkedin() == 0


@pytest.mark.parametrize("fail_transaction", [False, True])
def test_unpooled_sessions_close_connections_between_transactions(
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
    engine = create_unpooled_session_factory(
        "postgresql://runtime:secret@db.example/postgres"
    ).kw["bind"]
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
