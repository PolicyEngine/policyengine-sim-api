"""How sim records a v2 economy call's outcome: stored files, then result rows."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from policyengine_simulation_contract.call_reports import (
    CallEndedReport,
    CallReportStatus,
)
from policyengine_simulation_entry.economy_recorders import (
    SIMULATION_INPUT_ROLE,
    SIMULATION_OUTPUT_ROLE,
    ComparisonStartingRecorder,
    EconomySimulationRecorder,
    economy_result_recorders,
)
from policyengine_simulation_entry.simulation_database import SimulationDatabase
from policyengine_simulation_persistence import (
    EconomyComparisonRecorder,
    ReportKind,
    SimulationKind,
)
from policyengine_simulation_persistence.output_files import output_files

WRITTEN_AT = datetime(2026, 10, 27, 12, tzinfo=UTC)
RUN_ID = uuid4()


def _reference(uri: str, media_type: str, digest: str) -> dict:
    return {
        "uri": uri,
        "media_type": media_type,
        "content_sha256": digest,
        "size_bytes": 10,
    }


def _output(run_id=RUN_ID) -> dict:
    prefix = f"gs://private/stage-12-runs/staging/2026/10/{run_id}"
    return {
        "schema_version": 1,
        "simulation_run_id": str(run_id),
        "input": _reference(
            f"{prefix}/inputs/baseline-1.json", "application/json", "1" * 64
        ),
        "output": _reference(
            f"{prefix}/simulations/baseline-2.parquet",
            "application/vnd.apache.parquet",
            "2" * 64,
        ),
        "output_plan_sha256": "3" * 64,
        "row_identity": {
            "identifier_columns": ["household.household_id"],
            "row_count": 2,
            "identity_sha256": "4" * 64,
        },
        "bundle": {
            "policyengine_version": "6.7.5",
            "country_package_name": "policyengine-us",
            "country_package_version": "1.764.6",
            "dataset": {
                "identity": "populace_us_2024",
                "uri": "hf://policyengine/data/populace_us_2024.h5@revision",
                "artifact_revision": "revision",
                "data_package_name": "populace-data",
                "data_package_version": "0.1.0",
            },
            "bundle_manifest_sha256": "b" * 64,
        },
    }


class _Connection:
    """Record the statements and rows a writer executes."""

    def __init__(self) -> None:
        self.executed: list[tuple[object, object]] = []

    def execute(self, statement, rows=None):
        self.executed.append((statement, rows))


def test_a_finished_simulation_call_records_the_files_it_stored() -> None:
    recorder = EconomySimulationRecorder(clock=lambda: WRITTEN_AT)
    connection = _Connection()

    result = recorder.validate(_output())
    recorder.write(connection, (SimpleNamespace(id=RUN_ID),), result)

    ((statement, rows),) = connection.executed
    assert statement.table is output_files
    assert [
        (row["simulation_run_id"], row["role"], row["uri"], row["sha256"])
        for row in rows
    ] == [
        (RUN_ID, SIMULATION_INPUT_ROLE, result.input.uri, "1" * 64),
        (RUN_ID, SIMULATION_OUTPUT_ROLE, result.output.uri, "2" * 64),
    ]
    assert {row["written_at"] for row in rows} == {WRITTEN_AT}


def test_an_output_that_is_not_a_simulation_output_fails_validation() -> None:
    with pytest.raises(ValueError, match="validation error"):
        EconomySimulationRecorder().validate({"budget": {}})


def test_sim_records_economy_simulations_and_comparisons() -> None:
    recorders = economy_result_recorders()

    assert isinstance(
        recorders.simulations[SimulationKind.ECONOMY], EconomySimulationRecorder
    )
    assert isinstance(
        recorders.comparisons[ReportKind.ECONOMY], EconomyComparisonRecorder
    )


def test_the_simulation_database_records_economy_outcomes() -> None:
    database = SimulationDatabase.from_url(
        "postgresql://simulation_runtime:secret@db.example:5432/postgres"
    )

    recorders = database.call_recorder().recorders

    assert SimulationKind.ECONOMY in recorders.simulations
    assert ReportKind.ECONOMY in recorders.comparisons
    database.dispose()


class _Recorder:
    def __init__(self, answer: CallReportStatus) -> None:
        self.answer = answer
        self.reports: list[CallEndedReport] = []

    async def record_reported_call(self, report: CallEndedReport) -> CallReportStatus:
        self.reports.append(report)
        return self.answer


class _Comparisons:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.started_after: list[tuple] = []

    async def start_comparisons_after(self, simulation_run_ids) -> None:
        self.started_after.append(tuple(simulation_run_ids))
        if self.failure is not None:
            raise self.failure


SIMULATION_REPORT = CallEndedReport(
    modal_call_id="fc-sim", simulation_run_ids=(RUN_ID,)
)
COMPARISON_REPORT = CallEndedReport(modal_call_id="fc-cmp", report_run_id=uuid4())


@pytest.mark.parametrize(
    "answer", [CallReportStatus.RECORDED, CallReportStatus.ALREADY_RECORDED]
)
def test_a_recorded_simulation_call_starts_the_comparisons_it_completes(
    answer,
) -> None:
    comparisons = _Comparisons()
    recorder = ComparisonStartingRecorder(_Recorder(answer), comparisons)

    assert asyncio.run(recorder.record_reported_call(SIMULATION_REPORT)) is answer
    assert comparisons.started_after == [(RUN_ID,)]


@pytest.mark.parametrize(
    ("report", "answer"),
    [
        (SIMULATION_REPORT, CallReportStatus.STILL_RUNNING),
        (SIMULATION_REPORT, CallReportStatus.NOT_WRITTEN),
        (COMPARISON_REPORT, CallReportStatus.RECORDED),
    ],
)
def test_nothing_starts_until_a_simulation_calls_outcome_is_recorded(
    report, answer
) -> None:
    comparisons = _Comparisons()
    recorder = ComparisonStartingRecorder(_Recorder(answer), comparisons)

    asyncio.run(recorder.record_reported_call(report))

    assert comparisons.started_after == []


def test_a_comparison_that_cannot_start_leaves_the_answer_unchanged(caplog) -> None:
    comparisons = _Comparisons(failure=ConnectionError("database"))
    recorder = ComparisonStartingRecorder(
        _Recorder(CallReportStatus.RECORDED), comparisons
    )

    answer = asyncio.run(recorder.record_reported_call(SIMULATION_REPORT))

    assert answer is CallReportStatus.RECORDED
    assert "report_run_comparisons_not_started" in caplog.text


def test_the_simulation_database_starts_economy_calls_in_its_modal_environment() -> (
    None
):
    database = SimulationDatabase.from_url(
        "postgresql://simulation_runtime:secret@db.example:5432/postgres"
    )

    services = database.economy_routes(
        artifact_environment="staging", modal_environment="staging"
    )

    assert services.reports.artifact_environment == "staging"
    assert services.reports.dispatch.dispatcher._environment == "staging"
    database.dispose()
