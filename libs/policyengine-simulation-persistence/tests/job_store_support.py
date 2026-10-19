"""Builders shared by the job store tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from policyengine_simulation_persistence import create_simulation_engine
from policyengine_simulation_persistence.jobs import JobStore, NewJob, input_sha256

START = datetime(2026, 10, 20, 12, tzinfo=UTC)


class SteppingClock:
    """A clock the test moves forward explicitly."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **elapsed: float) -> None:
        self.now += timedelta(**elapsed)


def new_job(request: dict | None = None, **overrides) -> NewJob:
    """Return a household calculation job for ``request``."""
    values = {
        "kind": "household_calculation",
        "submitted_by": "api-v1-client@clients",
        "input_sha256": input_sha256(
            kind="household_calculation",
            policyengine_version="6.7.5",
            request=request or {"household": {"people": {"you": {"age": 40}}}},
        ),
        "policyengine_version": "6.7.5",
        "modal_application": "policyengine-simulation-v2-py6-7-5",
        "modal_function": "calculate_household",
        "observability_id": "00000000-0000-4000-8000-000000000001",
    }
    values.update(overrides)
    return NewJob(**values)


def runtime_store(database, clock=None) -> JobStore:
    """Return a store that connects as the runtime role, as the entrypoint does."""
    return JobStore(
        create_simulation_engine(database.runtime_url),
        clock=clock or SteppingClock(),
    )


def running_job(store: JobStore, request: dict | None = None):
    """Submit a job and take it through dispatch, as the entrypoint would."""
    job = store.submit(new_job(request)).job
    assert store.claim_dispatch(job.id)
    assert store.record_dispatched(job.id, modal_call_id=f"fc-{job.id.hex}")
    return store.get(job.id)
