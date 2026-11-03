"""The simulation service's database: readiness reporting and lifetime."""

from __future__ import annotations

import asyncio
import contextlib
import threading

import pytest
from conftest import FakeBackend, make_settings
from fastapi.testclient import TestClient
from policyengine_simulation_entry import app as app_module
from policyengine_simulation_entry import simulation_database as database_module
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.simulation_database import SimulationDatabase
from sqlalchemy.exc import OperationalError

DATABASE_URL = "postgresql://simulation_service:secret@db.example:5432/postgres"


class FakeSimulationDatabase:
    """Name the privileges the role lacks, or fail as an unreachable database does."""

    def __init__(self, *, missing_privileges=(), failure=None, delay_seconds=0):
        self.missing = list(missing_privileges)
        self.failure = failure
        self.delay_seconds = delay_seconds
        self.checks = 0
        self.disposed = False

    async def missing_privileges(self):
        self.checks += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.failure is not None:
            raise self.failure
        return self.missing

    def dispose(self):
        self.disposed = True


def _client(backend, database):
    return TestClient(
        create_app(
            settings=make_settings(simulation_database_url=DATABASE_URL),
            backend=backend,
            auth_dependency=lambda: None,
            simulation_database=database,
        )
    )


def test_readiness_reports_a_reached_database_with_every_privilege():
    database = FakeSimulationDatabase()
    with _client(FakeBackend(), database) as client:
        result = client.get("/ready")

    assert result.status_code == 200
    assert result.json() == {
        "status": "ready",
        "simulation_database": {"reachable": True, "missing_privileges": []},
    }
    assert database.checks == 1


def test_readiness_names_the_privileges_the_role_lacks():
    database = FakeSimulationDatabase(
        missing_privileges=["INSERT on reports", "SELECT on policies"]
    )
    with _client(FakeBackend(), database) as client:
        result = client.get("/ready")

    assert result.status_code == 200
    assert result.json() == {
        "status": "ready",
        "simulation_database": {
            "reachable": True,
            "missing_privileges": ["INSERT on reports", "SELECT on policies"],
        },
    }


def test_an_unreachable_database_does_not_change_old_gateway_readiness(caplog):
    failure = OperationalError("SELECT 1", {}, Exception("password=secret"))
    with _client(FakeBackend(), FakeSimulationDatabase(failure=failure)) as client:
        result = client.get("/ready")

    assert result.status_code == 200
    assert result.json() == {
        "status": "ready",
        "simulation_database": {"reachable": False, "missing_privileges": None},
    }
    assert "simulation_database_unreachable" in caplog.text
    assert "secret" not in caplog.text


def test_readiness_waits_only_briefly_for_the_database(monkeypatch):
    monkeypatch.setattr(
        app_module, "SIMULATION_DATABASE_READINESS_TIMEOUT_SECONDS", 0.01
    )
    slow_database = FakeSimulationDatabase(delay_seconds=1)
    with _client(FakeBackend(), slow_database) as client:
        database = client.get("/ready").json()["simulation_database"]

    assert database == {"reachable": False, "missing_privileges": None}


def test_readiness_still_follows_the_old_gateway():
    backend = FakeBackend()
    backend.is_ready = False
    with _client(backend, FakeSimulationDatabase()) as client:
        result = client.get("/ready")

    assert result.status_code == 503
    assert result.json() == {
        "status": "not_ready",
        "simulation_database": {"reachable": True, "missing_privileges": []},
    }


def test_readiness_keeps_its_old_shape_without_a_database():
    with TestClient(
        create_app(
            settings=make_settings(),
            backend=FakeBackend(),
            auth_dependency=lambda: None,
        )
    ) as client:
        result = client.get("/ready")

    assert result.status_code == 200
    assert result.json() == {"status": "ready"}


def test_the_database_is_released_at_shutdown():
    database = FakeSimulationDatabase()
    with _client(FakeBackend(), database):
        assert database.disposed is False

    assert database.disposed is True


def test_settings_build_the_database_only_when_it_is_configured(monkeypatch):
    built_urls = []

    def build(database_url):
        built_urls.append(database_url)
        return FakeSimulationDatabase()

    monkeypatch.setattr(SimulationDatabase, "from_url", staticmethod(build))

    create_app(settings=make_settings(), backend=FakeBackend())
    assert built_urls == []

    create_app(
        settings=make_settings(simulation_database_url=DATABASE_URL),
        backend=FakeBackend(),
    )
    assert built_urls == [DATABASE_URL]


def test_the_database_reads_the_missing_privileges_off_the_event_loop(monkeypatch):
    connections = []
    query_threads = []

    class FakeConnection:
        def __enter__(self):
            connections.append(self)
            return self

        def __exit__(self, *_):
            return False

        def begin(self):
            return contextlib.nullcontext()

        def execute(self, statement):
            return None

    class FakeEngine:
        disposed = False

        def connect(self):
            return FakeConnection()

        def dispose(self):
            self.disposed = True

    def missing_table_privileges(connection):
        assert connection in connections
        query_threads.append(threading.get_ident())
        return ["INSERT on reports"]

    monkeypatch.setattr(
        database_module, "missing_table_privileges", missing_table_privileges
    )
    engine = FakeEngine()
    database = SimulationDatabase(engine)

    async def read_from_the_event_loop():
        return await database.missing_privileges(), threading.get_ident()

    missing, event_loop_thread = asyncio.run(read_from_the_event_loop())
    database.dispose()

    assert missing == ["INSERT on reports"]
    assert query_threads != [event_loop_thread]
    assert engine.disposed is True


@pytest.mark.parametrize("database_url", ["", "postgres://user:secret@db/postgres"])
def test_a_malformed_database_url_never_builds_an_engine(database_url):
    with pytest.raises(ValueError, match="Simulation database URL") as error:
        SimulationDatabase.from_url(database_url)

    assert "secret" not in str(error.value)


def test_the_readiness_query_is_bounded_on_the_server(monkeypatch):
    """A stalled database can't hold a worker thread past the statement limit."""
    executed = []

    class FakeTransaction:
        def __enter__(self):
            executed.append("BEGIN")
            return self

        def __exit__(self, *_):
            executed.append("END")
            return False

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def begin(self):
            return FakeTransaction()

        def execute(self, statement):
            compiled = statement.compile()
            executed.append((str(compiled), dict(compiled.params)))

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    def missing_table_privileges(connection):
        executed.append("privileges")
        return []

    monkeypatch.setattr(
        database_module, "missing_table_privileges", missing_table_privileges
    )

    asyncio.run(SimulationDatabase(FakeEngine()).missing_privileges())

    assert executed[0] == "BEGIN"
    statement, parameters = executed[1]
    assert "set_config" in statement
    assert sorted(parameters.values(), key=str) == sorted(
        [
            "statement_timeout",
            str(database_module.READINESS_STATEMENT_TIMEOUT_MILLISECONDS),
            True,
        ],
        key=str,
    )
    assert executed[2:] == ["privileges", "END"]
    assert (
        database_module.READINESS_STATEMENT_TIMEOUT_MILLISECONDS
        < app_module.SIMULATION_DATABASE_READINESS_TIMEOUT_SECONDS * 1000
    )
