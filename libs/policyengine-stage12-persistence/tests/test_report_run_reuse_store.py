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
from sqlalchemy import MetaData, Table, delete, insert, update

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


def _regions(seeder) -> Table:
    return Table("regions", MetaData(schema="public"), autoload_with=seeder.engine)


def _region_of(seeder, run_id):
    """Insert a state region of the run's model version and dataset, and set it."""
    run = seeder.row("simulation_runs", run_id)
    region_id = uuid4()
    simulation_runs = seeder.table("simulation_runs")
    with seeder.engine.begin() as connection:
        connection.execute(
            insert(_regions(seeder)).values(
                id=region_id,
                code=f"state/test-{region_id.hex[:8]}",
                label="Test state",
                region_type="state",
                requires_filter=False,
                default_dataset_id=run["dataset_id"],
                tax_benefit_model_version_id=run["tax_benefit_model_version_id"],
            )
        )
        connection.execute(
            update(simulation_runs)
            .where(simulation_runs.c.id == run_id)
            .values(region_id=region_id)
        )
    return region_id


def _remove_region(seeder, region_id) -> None:
    regions = _regions(seeder)
    with seeder.engine.begin() as connection:
        connection.execute(delete(regions).where(regions.c.id == region_id))


def test_a_simulation_run_is_read_with_what_it_calculated_and_its_output_file(
    store, seeder, seeded_run: SeededReportRun
) -> None:
    run_id = seeded_run.run_ids.baseline_simulation_run_id
    region_id = _region_of(seeder, run_id)
    try:
        _read_before_and_after_success(store, seeder, run_id, region_id)
    finally:
        # The seeded run's dataset can't be removed while a region names it.
        _remove_region(seeder, region_id)


def _read_before_and_after_success(store, seeder, run_id, region_id) -> None:
    dataset_id = seeder.row("simulation_runs", run_id)["dataset_id"]

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

    assert dataset_id is not None
    assert pending == SimulationRunState(
        status=RunStatus.PENDING,
        year=2026,
        country_package_version=VERSIONS["country_package_version"],
        policyengine_version=VERSIONS["policyengine_version"],
        dataset_id=dataset_id,
        region_id=region_id,
        output_uri=None,
        output_sha256=None,
    )
    assert succeeded == SimulationRunState(
        status=RunStatus.SUCCEEDED,
        year=2026,
        country_package_version=VERSIONS["country_package_version"],
        policyengine_version=VERSIONS["policyengine_version"],
        dataset_id=dataset_id,
        region_id=region_id,
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
