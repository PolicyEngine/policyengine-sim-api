"""The safety check: one recording pass per execution of a Cloud Run job."""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta

import pytest
from policyengine_simulation_entry import safety_check
from policyengine_simulation_entry.run_dispatch import FIRST_CHECK_DELAY
from policyengine_simulation_entry.run_recording import (
    CHECK_LEASE,
    SAFETY_CHECK_INTERVAL,
    RecordingPass,
)

DATABASE_URL = "postgresql://runtime:secret@db.example/postgres"


class _OnePass:
    """A recorder whose pass returns a scripted result, or raises."""

    def __init__(self, result) -> None:
        self.result = result
        self.passes = 0

    async def record_due_outcomes(self) -> RecordingPass:
        self.passes += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _Telemetry:
    """Stands in for the observability runtime the job starts and shuts down."""

    def __init__(self) -> None:
        self.environments = []
        self.shut_down = False

    def start(self, environment):
        self.environments.append(environment)
        return self

    def shutdown(self) -> None:
        self.shut_down = True


def _run(environment, recorder, telemetry=None):
    built_for = []

    def build(database_url):
        built_for.append(database_url)
        return recorder

    telemetry = telemetry or _Telemetry()
    status = safety_check.main(
        environment=environment,
        build_recorder=build,
        start_observability=telemetry.start,
    )
    return status, built_for


def test_each_execution_runs_one_pass_and_exits() -> None:
    recorder = _OnePass(RecordingPass(checked=4, succeeded=1, failed=1))

    status, built_for = _run({"SIMULATION_DATABASE_URL": DATABASE_URL}, recorder)

    assert status == 0
    assert recorder.passes == 1
    assert built_for == [DATABASE_URL]


def test_the_pass_is_logged_with_what_it_found(caplog) -> None:
    caplog.set_level("INFO")
    recorder = _OnePass(RecordingPass(checked=4, succeeded=1, abandoned=2))

    _run({"SIMULATION_DATABASE_URL": DATABASE_URL}, recorder)

    (record,) = [
        record for record in caplog.records if record.message == "safety_check_pass"
    ]
    # The observability log handler keeps only these attributes of a record.
    assert record.policyengine_attributes["checked"] == 4
    assert record.policyengine_attributes["succeeded"] == 1
    assert record.policyengine_attributes["abandoned"] == 2


def test_the_job_logs_through_the_observability_runtime_and_shuts_it_down() -> None:
    telemetry = _Telemetry()

    _run(
        {"SIMULATION_DATABASE_URL": DATABASE_URL, "APP_ENVIRONMENT": "production"},
        _OnePass(RecordingPass()),
        telemetry,
    )

    assert telemetry.environments == ["production"]
    assert telemetry.shut_down


def test_an_execution_without_the_database_url_fails_without_a_pass() -> None:
    recorder = _OnePass(RecordingPass())

    status, built_for = _run({}, recorder)

    assert status == safety_check.EXIT_NOT_CONFIGURED
    assert built_for == []


def test_a_pass_that_raises_fails_the_execution() -> None:
    status, _ = _run(
        {"SIMULATION_DATABASE_URL": DATABASE_URL}, _OnePass(RuntimeError("down"))
    )

    assert status == safety_check.EXIT_PASS_FAILED


def test_a_signal_to_the_job_is_not_swallowed() -> None:
    with pytest.raises(KeyboardInterrupt):
        _run({"SIMULATION_DATABASE_URL": DATABASE_URL}, _OnePass(KeyboardInterrupt()))


def test_the_job_runs_the_module_as_its_command() -> None:
    """The deploy runs ``python -m policyengine_simulation_entry.safety_check``."""
    result = subprocess.run(
        [sys.executable, "-m", "policyengine_simulation_entry.safety_check"],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == safety_check.EXIT_NOT_CONFIGURED
    assert "SIMULATION_DATABASE_URL" in result.stderr


def test_a_call_is_first_checked_one_interval_after_dispatch() -> None:
    """A call reports when it ends; the safety check looks only after a while."""
    assert timedelta(minutes=5) == SAFETY_CHECK_INTERVAL
    assert FIRST_CHECK_DELAY == SAFETY_CHECK_INTERVAL
    assert CHECK_LEASE == SAFETY_CHECK_INTERVAL
