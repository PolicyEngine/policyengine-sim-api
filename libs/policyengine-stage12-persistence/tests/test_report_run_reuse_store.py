"""Reading a simulation run that a report run reuses, as the Modal worker role.

The simulation service may name, as a report run's baseline or reform, a
simulation run that already succeeded. The coordinator reads that run's
status, what it calculated and its output file instead of calculating it
again. These run against a disposable Postgres that the Public API's v2
migrations created (see ``report_run_database``).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from policyengine_stage12_persistence import (
    ReportRunStore,
    RunStatus,
    SimulationRunState,
)
from report_run_database import NOW, VERSIONS, SeededReportRun, worker_database_url

LATER = NOW + timedelta(minutes=5)
OUTPUT_URI = "gs://detailed-save-outputs/simulations/baseline.parquet"
OUTPUT_SHA256 = "d" * 64


@pytest.fixture
def store():
    run_store = ReportRunStore(worker_database_url())
    try:
        yield run_store
    finally:
        run_store.engine.dispose()


def test_a_simulation_run_is_read_with_what_it_calculated_and_its_output_file(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    run_id = seeded_run.run_ids.baseline_simulation_run_id

    pending = store.get_simulation_run(run_id)
    store.claim_simulation_run(run_id, modal_call_id="fc-worker", claimed_at=NOW)
    store.complete_simulation_run(
        run_id,
        output_uri=OUTPUT_URI,
        output_sha256=OUTPUT_SHA256,
        completed_at=LATER,
    )
    stored_before_reads = seeder.row("simulation_runs", run_id)
    succeeded = store.get_simulation_run(run_id)
    read_again = store.get_simulation_run(run_id)

    assert pending == SimulationRunState(
        status=RunStatus.PENDING,
        year=2026,
        country_package_version=VERSIONS["country_package_version"],
        policyengine_version=VERSIONS["policyengine_version"],
        output_uri=None,
        output_sha256=None,
    )
    assert succeeded == SimulationRunState(
        status=RunStatus.SUCCEEDED,
        year=2026,
        country_package_version=VERSIONS["country_package_version"],
        policyengine_version=VERSIONS["policyengine_version"],
        output_uri=OUTPUT_URI,
        output_sha256=OUTPUT_SHA256,
    )
    assert read_again == succeeded
    # Reading a run changes nothing about it.
    assert seeder.row("simulation_runs", run_id) == stored_before_reads


def test_reading_a_simulation_run_that_does_not_exist_raises_lookup_error(
    store,
) -> None:
    with pytest.raises(LookupError, match="simulation run"):
        store.get_simulation_run(uuid4())
