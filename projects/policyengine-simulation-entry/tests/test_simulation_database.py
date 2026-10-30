"""The Simulation Entrypoint's own database: readiness reporting and lifetime."""

from __future__ import annotations

import asyncio

import pytest
from conftest import FakeBackend, make_settings
from fastapi.testclient import TestClient
from policyengine_simulation_entry import app as app_module
from policyengine_simulation_entry import simulation_database as database_module
from policyengine_simulation_entry.app import RunServices, create_app
from policyengine_simulation_entry.simulation_database import SimulationDatabase
from policyengine_simulation_persistence import HEAD_REVISION
from sqlalchemy.exc import OperationalError

DATABASE_URL = "postgresql://simulation_runtime:secret@db.example:5432/postgres"


class FakeSimulationDatabase:
    """Report a fixed revision, or fail the way an unreachable database does."""

    def __init__(self, *, revision=HEAD_REVISION, failure=None, delay_seconds=0):
        self.revision = revision
        self.failure = failure
        self.delay_seconds = delay_seconds
        self.disposed = False

    async def read_revision(self):
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.failure is not None:
            raise self.failure
        return self.revision

    def dispose(self):
        self.disposed = True


def _client(backend, database):
    return TestClient(
        create_app(
            settings=make_settings(simulation_database_url=DATABASE_URL),
            backend=backend,
            auth_dependency=lambda: None,
            run_services=RunServices(database=database),
        )
    )


def test_readiness_reports_the_database_revision_and_the_expected_one():
    backend = FakeBackend()
    with _client(backend, FakeSimulationDatabase(revision="0001")) as client:
        result = client.get("/ready")

    assert result.status_code == 200
    assert result.json() == {
        "status": "ready",
        "simulation_database": {
            "reachable": True,
            "revision": "0001",
            "expected_revision": HEAD_REVISION,
        },
    }


def test_readiness_reports_a_database_that_has_no_migrations_yet():
    with _client(FakeBackend(), FakeSimulationDatabase(revision=None)) as client:
        database = client.get("/ready").json()["simulation_database"]

    assert database == {
        "reachable": True,
        "revision": None,
        "expected_revision": HEAD_REVISION,
    }


def test_an_unreachable_database_does_not_change_old_gateway_readiness(caplog):
    failure = OperationalError("SELECT 1", {}, Exception("password=secret"))
    with _client(FakeBackend(), FakeSimulationDatabase(failure=failure)) as client:
        result = client.get("/ready")

    assert result.status_code == 200
    assert result.json() == {
        "status": "ready",
        "simulation_database": {
            "reachable": False,
            "revision": None,
            "expected_revision": HEAD_REVISION,
        },
    }
    assert "secret" not in caplog.text


def test_readiness_waits_only_briefly_for_the_database(monkeypatch):
    monkeypatch.setattr(
        app_module, "SIMULATION_DATABASE_READINESS_TIMEOUT_SECONDS", 0.01
    )
    slow_database = FakeSimulationDatabase(delay_seconds=1)
    with _client(FakeBackend(), slow_database) as client:
        database = client.get("/ready").json()["simulation_database"]

    assert database["reachable"] is False


def test_readiness_still_follows_the_old_gateway():
    backend = FakeBackend()
    backend.is_ready = False
    with _client(backend, FakeSimulationDatabase()) as client:
        result = client.get("/ready")

    assert result.status_code == 503
    assert result.json()["status"] == "not_ready"
    assert result.json()["simulation_database"]["reachable"] is True


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


def test_the_database_reads_the_revision_without_blocking_the_event_loop(
    monkeypatch,
):
    connections = []

    class FakeConnection:
        def __enter__(self):
            connections.append(self)
            return self

        def __exit__(self, *_):
            return False

    class FakeEngine:
        disposed = False

        def connect(self):
            return FakeConnection()

        def dispose(self):
            self.disposed = True

    monkeypatch.setattr(
        database_module,
        "read_schema_revision",
        lambda connection: "0001" if connection in connections else None,
    )
    engine = FakeEngine()
    database = SimulationDatabase(engine)

    assert asyncio.run(database.read_revision()) == "0001"
    database.dispose()
    assert engine.disposed is True


@pytest.mark.parametrize("database_url", ["", "postgres://user:secret@db/postgres"])
def test_a_malformed_database_url_never_builds_an_engine(database_url):
    with pytest.raises(ValueError, match="Simulation database URL") as error:
        SimulationDatabase.from_url(database_url)

    assert "secret" not in str(error.value)
