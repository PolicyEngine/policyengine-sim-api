"""The simulation service's database: readiness reporting and lifetime."""

from __future__ import annotations

import asyncio
import threading

import pytest
from conftest import FakeBackend, make_settings
from fastapi.testclient import TestClient
from policyengine_observability import TelemetryLimits
from policyengine_simulation_entry import app as app_module
from policyengine_simulation_entry import simulation_database as database_module
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.simulation_database import (
    MISSING_PRIVILEGES_LOG_EVENT,
    SimulationDatabase,
    SimulationDatabaseReadinessCheck,
    missing_privilege_log_messages,
)
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


def _log_messages(caplog):
    """Each record's message, the only text the observability runtime keeps.

    Its log handler drops a record's ``extra`` fields, so what an operator
    needs has to be in the message itself.
    """
    return [record.getMessage() for record in caplog.records]


def test_readiness_reports_a_reached_database_with_every_privilege():
    database = FakeSimulationDatabase()
    with _client(FakeBackend(), database) as client:
        result = client.get("/ready")

    assert result.status_code == 200
    assert result.json() == {
        "status": "ready",
        "simulation_database": {"reachable": True, "has_required_privileges": True},
    }
    assert database.checks == 1


def test_readiness_says_only_that_privileges_are_missing_and_logs_their_names(
    caplog,
):
    """The route is public, so the names go to the service's own log."""
    database = FakeSimulationDatabase(
        missing_privileges=["INSERT on reports", "SELECT on policies"]
    )
    with _client(FakeBackend(), database) as client:
        result = client.get("/ready")

    assert result.status_code == 200
    assert result.json() == {
        "status": "ready",
        "simulation_database": {"reachable": True, "has_required_privileges": False},
    }
    assert "INSERT on reports" not in result.text
    assert (
        f"{MISSING_PRIVILEGES_LOG_EVENT}: INSERT on reports, SELECT on policies"
        in _log_messages(caplog)
    )


def test_a_role_with_every_privilege_logs_nothing_about_privileges(caplog):
    with _client(FakeBackend(), FakeSimulationDatabase()) as client:
        client.get("/ready")

    assert not any(
        MISSING_PRIVILEGES_LOG_EVENT in message for message in _log_messages(caplog)
    )


def test_missing_privileges_are_logged_in_messages_the_log_keeps_whole():
    """The observability runtime cuts a message at its string limit."""
    missing = [f"INSERT on modelled_table_number_{number}" for number in range(80)]

    messages = missing_privilege_log_messages(missing)

    assert len(messages) > 1
    assert all(
        len(message) <= TelemetryLimits().max_string_length for message in messages
    )
    assert all(
        message.startswith(f"{MISSING_PRIVILEGES_LOG_EVENT}: ") for message in messages
    )
    logged = [
        name
        for message in messages
        for name in message.removeprefix(f"{MISSING_PRIVILEGES_LOG_EVENT}: ").split(
            ", "
        )
    ]
    assert logged == missing


def test_an_unreachable_database_does_not_change_old_gateway_readiness(caplog):
    failure = OperationalError("SELECT 1", {}, Exception("password=secret"))
    with _client(FakeBackend(), FakeSimulationDatabase(failure=failure)) as client:
        result = client.get("/ready")

    assert result.status_code == 200
    assert result.json() == {
        "status": "ready",
        "simulation_database": {"reachable": False, "has_required_privileges": None},
    }
    assert "simulation_database_unreachable: OperationalError" in _log_messages(caplog)
    assert "secret" not in caplog.text


def test_readiness_waits_only_briefly_for_the_database(monkeypatch):
    monkeypatch.setattr(
        app_module, "SIMULATION_DATABASE_READINESS_TIMEOUT_SECONDS", 0.01
    )
    slow_database = FakeSimulationDatabase(delay_seconds=1)
    with _client(FakeBackend(), slow_database) as client:
        database = client.get("/ready").json()["simulation_database"]

    assert database == {"reachable": False, "has_required_privileges": None}


def test_readiness_still_follows_the_old_gateway():
    backend = FakeBackend()
    backend.is_ready = False
    with _client(backend, FakeSimulationDatabase()) as client:
        result = client.get("/ready")

    assert result.status_code == 503
    assert result.json() == {
        "status": "not_ready",
        "simulation_database": {"reachable": True, "has_required_privileges": True},
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


@pytest.mark.parametrize("database_url", ["", "postgres://user:secret@db/postgres"])
def test_a_malformed_database_url_never_builds_an_engine(database_url):
    with pytest.raises(ValueError, match="Simulation database URL") as error:
        SimulationDatabase.from_url(database_url)

    assert "secret" not in str(error.value)


def test_a_malformed_database_url_stops_the_app_with_a_configuration_error():
    """The settings' check runs before the engine is built, as at startup."""
    from policyengine_simulation_entry.config import ConfigurationError

    with pytest.raises(
        ConfigurationError, match="SIMULATION_SERVICE_DATABASE_URL"
    ) as error:
        create_app(
            settings=make_settings(
                simulation_database_url="postgresql://db.example/postgres?password=secret"
            ),
            backend=FakeBackend(),
        )

    assert "secret" not in str(error.value)


def test_the_check_runs_on_its_own_thread_within_the_database_time_limit(
    monkeypatch,
):
    calls = []

    def read_missing_table_privileges(engine, *, statement_timeout_milliseconds):
        calls.append(
            (engine, statement_timeout_milliseconds, threading.current_thread().name)
        )
        return ["INSERT on reports"]

    monkeypatch.setattr(
        database_module, "read_missing_table_privileges", read_missing_table_privileges
    )
    engine = object()
    database = SimulationDatabase(engine)

    try:
        missing = asyncio.run(database.missing_privileges())
    finally:
        database.executor.shutdown(wait=True)

    assert missing == ["INSERT on reports"]
    [(called_engine, timeout_milliseconds, thread_name)] = calls
    assert called_engine is engine
    expected_timeout = database_module.READINESS_STATEMENT_TIMEOUT_MILLISECONDS
    assert timeout_milliseconds == expected_timeout
    assert timeout_milliseconds < (
        app_module.SIMULATION_DATABASE_READINESS_TIMEOUT_SECONDS * 1000
    )
    assert thread_name.startswith(database_module.READINESS_THREAD_NAME_PREFIX)


def test_a_stalled_database_holds_one_thread_and_no_thread_of_the_default_pool(
    monkeypatch,
):
    """Checks that time out don't pile up threads other routes need."""
    release = threading.Event()
    running = []
    most_running_at_once = []
    lock = threading.Lock()

    def stall(engine, *, statement_timeout_milliseconds):
        with lock:
            running.append(threading.current_thread().name)
            most_running_at_once.append(len(running))
        release.wait(timeout=5)
        with lock:
            running.remove(threading.current_thread().name)
        return []

    monkeypatch.setattr(database_module, "read_missing_table_privileges", stall)
    database = SimulationDatabase(object())

    async def three_checks_that_time_out():
        results = await asyncio.gather(
            *(
                asyncio.wait_for(database.missing_privileges(), timeout=0.05)
                for _ in range(3)
            ),
            return_exceptions=True,
        )
        default_pool_threads = [
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("asyncio_")
        ]
        return results, default_pool_threads

    try:
        results, default_pool_threads = asyncio.run(three_checks_that_time_out())
    finally:
        release.set()
        database.executor.shutdown(wait=True)

    assert all(isinstance(result, TimeoutError) for result in results)
    # Only the first check ever ran: the two that timed out while it held the
    # thread were cancelled before they started, and never ran afterwards.
    assert most_running_at_once == [1]
    assert default_pool_threads == []


def test_disposing_the_database_also_stops_its_thread(monkeypatch):
    class FakeEngine:
        disposed = False

        def dispose(self):
            self.disposed = True

    monkeypatch.setattr(
        database_module,
        "read_missing_table_privileges",
        lambda engine, *, statement_timeout_milliseconds: [],
    )
    engine = FakeEngine()
    database = SimulationDatabase(engine)
    asyncio.run(database.missing_privileges())
    [worker] = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(database_module.READINESS_THREAD_NAME_PREFIX)
    ]

    database.dispose()
    worker.join(timeout=5)

    assert engine.disposed is True
    assert not worker.is_alive()


def test_a_disposed_database_reports_every_later_check_as_failed(monkeypatch):
    class FakeEngine:
        def dispose(self):
            return None

    monkeypatch.setattr(
        database_module,
        "read_missing_table_privileges",
        lambda engine, *, statement_timeout_milliseconds: [],
    )
    database = SimulationDatabase(FakeEngine())
    database.dispose()

    with _client(FakeBackend(), database) as client:
        result = client.get("/ready")

    assert result.json()["simulation_database"] == {
        "reachable": False,
        "has_required_privileges": None,
    }


# --- Reusing a check ------------------------------------------------------


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self):
        self.seconds = 1000.0

    def __call__(self):
        return self.seconds


def _readiness_check(database, clock):
    return SimulationDatabaseReadinessCheck(database, timeout_seconds=1, clock=clock)


def _check_readiness(check, times=1):
    async def check_in_order():
        return [await check.readiness() for _ in range(times)]

    return asyncio.run(check_in_order())


def test_the_check_is_reused_for_thirty_seconds_by_default():
    assert database_module.READINESS_CACHE_SECONDS == 30


def test_a_check_that_reached_the_database_is_reused_until_it_expires():
    database = FakeSimulationDatabase()
    clock = FakeClock()
    check = _readiness_check(database, clock)

    _check_readiness(check, times=3)
    clock.seconds += database_module.READINESS_CACHE_SECONDS - 0.1
    [reused] = _check_readiness(check)
    assert database.checks == 1

    clock.seconds += 0.1
    [fresh] = _check_readiness(check)
    assert database.checks == 2
    assert reused == fresh


def test_a_role_missing_privileges_is_reused_and_logged_once(caplog):
    database = FakeSimulationDatabase(missing_privileges=["INSERT on reports"])
    check = _readiness_check(database, FakeClock())

    results = _check_readiness(check, times=3)

    assert database.checks == 1
    assert {result.has_required_privileges for result in results} == {False}
    logged = [
        message
        for message in _log_messages(caplog)
        if message.startswith(MISSING_PRIVILEGES_LOG_EVENT)
    ]
    assert logged == [f"{MISSING_PRIVILEGES_LOG_EVENT}: INSERT on reports"]


@pytest.mark.parametrize("failure", [OperationalError("SELECT 1", {}, Exception())])
def test_a_failed_check_is_not_reused(failure):
    """A cold instance that reaches its database a moment later says so at once."""
    database = FakeSimulationDatabase(failure=failure)
    check = _readiness_check(database, FakeClock())

    results = _check_readiness(check, times=2)

    assert database.checks == 2
    assert {result.reachable for result in results} == {False}


def test_requests_that_arrive_during_a_check_share_it():
    database = FakeSimulationDatabase(delay_seconds=0.05)
    check = _readiness_check(database, FakeClock())

    async def three_requests_at_once():
        return await asyncio.gather(*(check.readiness() for _ in range(3)))

    results = asyncio.run(three_requests_at_once())

    assert database.checks == 1
    assert len(set(results)) == 1


def test_a_request_that_stops_waiting_leaves_the_shared_check_running():
    database = FakeSimulationDatabase(delay_seconds=0.05)
    check = _readiness_check(database, FakeClock())

    async def one_request_cancelled():
        cancelled = asyncio.ensure_future(check.readiness())
        waiting = asyncio.ensure_future(check.readiness())
        await asyncio.sleep(0)
        cancelled.cancel()
        return await waiting

    result = asyncio.run(one_request_cancelled())

    assert result.reachable is True
    assert database.checks == 1


def test_repeated_readiness_requests_check_the_database_once():
    database = FakeSimulationDatabase()
    with _client(FakeBackend(), database) as client:
        first = client.get("/ready").json()
        second = client.get("/ready").json()

    assert first == second
    assert database.checks == 1
