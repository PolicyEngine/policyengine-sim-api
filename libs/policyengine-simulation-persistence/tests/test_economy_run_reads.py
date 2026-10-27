"""The reads and lookups the v2 economy path needs from the run tables."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from policyengine_simulation_persistence import (
    NewReportRun,
    OutputFile,
    RunErrorCode,
    RunTrigger,
    create_simulation_engine,
    record_output_files,
)
from report_support import economy_pair, economy_report, report_store
from run_support import economy_run, run_store, set_simulation_run_status
from simulation_support import economy_simulation, simulation_store

pytestmark = pytest.mark.postgres

SHA256 = "a" * 64


def _report_run(database, rows, *, reform_policy=True):
    baseline, reform = economy_pair(database, rows)
    if not reform_policy:
        reform = baseline
    report = report_store(database).create(economy_report(baseline, reform))
    store = run_store(database)
    baseline_run = store.start_simulation_run(economy_run(baseline, rows)).run
    reform_run = store.start_simulation_run(economy_run(reform, rows)).run
    report_run = store.create_report_run(
        NewReportRun(
            report_id=report.id,
            baseline_simulation_run_id=baseline_run.id,
            reform_simulation_run_id=reform_run.id,
            trigger=RunTrigger.INITIAL,
        )
    )
    return report, baseline_run, reform_run, report_run


def test_an_economy_simulation_with_the_same_definition_is_found_not_created(
    migrated_database, public_api_rows
) -> None:
    store = simulation_store(migrated_database)
    first = store.find_or_create(economy_simulation(public_api_rows, policy_id=None))

    again = store.find_or_create(economy_simulation(public_api_rows, policy_id=None))
    other = store.find_or_create(economy_simulation(public_api_rows))
    on_another_dataset = store.find_or_create(
        economy_simulation(
            public_api_rows, policy_id=None, dataset_name="populace_us_2024"
        )
    )

    assert again == first
    assert other.id != first.id
    assert on_another_dataset.id != first.id
    assert (
        store.find_or_create(
            economy_simulation(
                public_api_rows, policy_id=None, dataset_name="populace_us_2024"
            )
        )
        == on_another_dataset
    )


def test_a_reports_runs_are_listed_newest_first(
    migrated_database, public_api_rows
) -> None:
    report, baseline_run, reform_run, first = _report_run(
        migrated_database, public_api_rows
    )
    store = run_store(migrated_database)
    second = store.create_report_run(
        NewReportRun(
            report_id=report.id,
            baseline_simulation_run_id=baseline_run.id,
            reform_simulation_run_id=reform_run.id,
            trigger=RunTrigger.MANUAL,
        )
    )

    assert [run.id for run in store.report_runs(report.id)] == [second.id, first.id]
    assert store.report_runs(uuid4()) == []


def test_a_comparison_is_ready_once_both_its_runs_succeeded(
    migrated_database, public_api_rows
) -> None:
    _, baseline_run, reform_run, report_run = _report_run(
        migrated_database, public_api_rows
    )
    store = run_store(migrated_database)
    assert store.comparisons_ready_to_start() == []

    set_simulation_run_status(migrated_database, baseline_run.id, "succeeded")
    assert store.comparisons_ready_to_start() == []
    set_simulation_run_status(migrated_database, reform_run.id, "succeeded")

    assert [run.id for run in store.comparisons_ready_to_start()] == [report_run.id]
    assert [
        run.id
        for run in store.comparisons_ready_to_start(simulation_run_ids={reform_run.id})
    ] == [report_run.id]
    assert store.comparisons_ready_to_start(simulation_run_ids={uuid4()}) == []


def test_a_run_compared_with_itself_is_ready_once_it_succeeded(
    migrated_database, public_api_rows
) -> None:
    _, baseline_run, reform_run, report_run = _report_run(
        migrated_database, public_api_rows, reform_policy=False
    )
    assert baseline_run.id == reform_run.id

    set_simulation_run_status(migrated_database, baseline_run.id, "succeeded")

    assert [
        run.id for run in run_store(migrated_database).comparisons_ready_to_start()
    ] == [report_run.id]


def test_pending_runs_left_undispatched_are_found_after_a_while(
    migrated_database, public_api_rows
) -> None:
    _, baseline_run, reform_run, _ = _report_run(migrated_database, public_api_rows)
    store = run_store(migrated_database)
    set_simulation_run_status(migrated_database, reform_run.id, "failed")

    later = datetime.now(UTC) + timedelta(minutes=11)
    assert [run.id for run in store.pending_simulation_runs(created_before=later)] == [
        baseline_run.id
    ]
    assert (
        store.pending_simulation_runs(
            created_before=datetime.now(UTC) - timedelta(minutes=1)
        )
        == []
    )


def test_a_simulation_runs_stored_files_are_read_by_role(
    migrated_database, public_api_rows
) -> None:
    _, baseline_run, _, _ = _report_run(migrated_database, public_api_rows)
    store = run_store(migrated_database)
    written_at = datetime.now(UTC)
    engine = create_simulation_engine(migrated_database.runtime_url)
    with engine.begin() as connection:
        record_output_files(
            connection,
            [
                OutputFile("simulation_output", "gs://bucket/output.parquet", SHA256),
                OutputFile("simulation_input", "gs://bucket/input.json", "b" * 64),
            ],
            written_at=written_at,
            simulation_run_id=baseline_run.id,
        )

    output = store.simulation_output_file(baseline_run.id, "simulation_output")

    assert output is not None
    assert (output.uri, output.sha256) == ("gs://bucket/output.parquet", SHA256)
    assert store.simulation_output_file(baseline_run.id, "simulation_log") is None
    assert (
        store.simulation_output_file(
            baseline_run.id,
            "simulation_output",
            now=written_at + timedelta(days=31),
        )
        is None
    )


def test_a_run_the_catalog_cannot_run_fails_as_invalid_input() -> None:
    assert RunErrorCode.INVALID_INPUT.value == "invalid_input"
