"""The Stage 12 coordinator records the status of the report run its call names."""

from __future__ import annotations

import pytest
from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from policyengine_simulation_contract.stage12_execution import (
    ComparisonRunLifecycleStatus,
)
from policyengine_simulation_executor.stage12_runtime import (
    coordination as coordination_module,
)
from policyengine_stage12_persistence import RunStatus
from stage12_child_call_support import ConcurrentInvoker, InjectedChildFaults
from stage12_fake_report_runs import (
    REFORM_RUN_ID,
    REPORT_RUN_ID,
    FakeReportRuns,
    economy_aggregate,
    report_run_context,
)
from stage12_fake_stores import FakeArtifacts, FakeStore
from stage12_runtime_support import (
    coordinate_report,
    parent_record,
    report_execution_input,
)

COORDINATOR_CALL_ID = "fc-coordinator"


class RecordingInvoker(ConcurrentInvoker):
    """Run both children concurrently, keeping the context each one received."""

    def __init__(self, artifacts, **options):
        super().__init__(artifacts, **options)
        self.contexts = []

    def spawn(self, request):
        self.contexts.append(request.context)
        return super().spawn(request)


def _coordinate(store, report_runs, invoker, artifacts, *, context=None):
    context = context or report_run_context()
    return coordinate_report(
        report_execution_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id=COORDINATOR_CALL_ID,
        store=store,
        artifacts=artifacts,
        invoker=invoker,
        aggregator=economy_aggregate,
        report_runs=report_runs,
    )


def _recording_report_writes(store, events):
    replace_report = store.replace_report

    def recording_replace_report(record):
        events.append(f"stage12:{record.status.value}")
        return replace_report(record)

    store.replace_report = recording_replace_report
    return store


def test_coordinator_claims_and_completes_its_report_run_before_stage12() -> None:
    events: list[str] = []
    store = _recording_report_writes(FakeStore(), events)
    report_runs = FakeReportRuns(events)
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(artifacts)

    result = _coordinate(store, report_runs, invoker, artifacts)

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.SUCCEEDED
    assert run.modal_call_id == COORDINATOR_CALL_ID
    assert result["evaluation_id"] == str(report_execution_input().evaluation_id)
    # The report run is claimed before, and completed before, the Stage 12
    # report, so a restart between the two finds the Stage 12 report running.
    assert events == ["claim_report_run", "complete_report_run", "stage12:succeeded"]
    assert store.parent.status is ComparisonRunLifecycleStatus.SUCCEEDED
    assert [context["report_run"] for context in invoker.contexts] == [
        report_run_context().report_run.model_dump(mode="json")
    ] * 2


def test_coordinator_completes_its_report_run_with_the_economy_output_as_rows() -> None:
    report_runs = FakeReportRuns()
    artifacts = FakeArtifacts()

    _coordinate(FakeStore(), report_runs, RecordingInvoker(artifacts), artifacts)

    result = report_runs.report_runs[REPORT_RUN_ID].result
    rows = {rows.table.name: rows.rows for rows in result.rows}
    budget = {row["variable_name"]: row for row in rows["budget_summary"]}
    assert budget["household_tax"]["change"] == 100.0
    assert budget["household_tax"]["report_run_id"] == REPORT_RUN_ID
    # The shared report is a US one, so its poverty rows use the SPM types.
    assert {row["poverty_type"] for row in rows["poverty"]} == {"spm", "spm_deep"}
    assert (
        result.labor_supply_result
        == (CURRENT_SINGLE_YEAR_MACRO_RESULT["labor_supply_response"])
    )
    # Alabama's first district, by .py's state FIPS codes.
    (district,) = rows["congressional_district_impacts"]
    assert (district["state_fips"], district["district_number"]) == (1, 1)
    assert district["district_geoid"] == 101


def test_comparison_children_receive_the_context_they_received_before() -> None:
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(artifacts)
    report_runs = FakeReportRuns()

    _coordinate(
        FakeStore(),
        report_runs,
        invoker,
        artifacts,
        context=report_run_context().model_copy(update={"report_run": None}),
    )

    assert all("report_run" not in context for context in invoker.contexts)
    assert report_runs.events == []


@pytest.mark.parametrize(
    ("status", "modal_call_id"),
    [
        (RunStatus.RUNNING, "fc-another-coordinator"),
        (RunStatus.SUCCEEDED, COORDINATOR_CALL_ID),
        (RunStatus.FAILED, COORDINATOR_CALL_ID),
    ],
)
def test_a_report_run_that_cannot_be_claimed_starts_nothing(
    status, modal_call_id
) -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()
    run = report_runs.report_runs[REPORT_RUN_ID]
    run.status, run.modal_call_id = status, modal_call_id
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(artifacts)

    result = _coordinate(store, report_runs, invoker, artifacts)

    assert result == {
        "deduplicated": True,
        "report_run_id": str(REPORT_RUN_ID),
        "status": status.value,
    }
    assert invoker.events == []
    assert store.parent is None
    assert run.writes == ["claim_report_run"]


def test_a_report_run_must_combine_the_simulation_runs_its_call_names() -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()
    report_runs.links = {("baseline", REFORM_RUN_ID)}
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(artifacts)

    with pytest.raises(ValueError, match="does not combine"):
        _coordinate(store, report_runs, invoker, artifacts)

    assert invoker.events == []
    assert store.parent is None
    assert report_runs.report_runs[REPORT_RUN_ID].status is RunStatus.PENDING


def test_a_failed_report_fails_its_report_run_and_the_failed_simulation_run() -> None:
    store = FakeStore()
    report_runs = FakeReportRuns()
    artifacts = FakeArtifacts()
    invoker = RecordingInvoker(
        artifacts, faults=InjectedChildFaults(fail_role="reform")
    )

    with pytest.raises(RuntimeError, match="coordination failed"):
        _coordinate(store, report_runs, invoker, artifacts)

    run = report_runs.report_runs[REPORT_RUN_ID]
    assert run.status is RunStatus.FAILED
    assert run.error_message.startswith("simulation_invocation_failed: ")
    reform = report_runs.simulation_runs[REFORM_RUN_ID]
    assert reform.status is RunStatus.FAILED
    assert reform.error_message == run.error_message
    assert store.parent.status is ComparisonRunLifecycleStatus.FAILED


def test_report_run_coordinator_names_its_aggregate_by_digest(monkeypatch) -> None:
    built = []
    artifacts = FakeArtifacts()

    def recording_artifact_store(*, content_addressed=False):
        built.append(content_addressed)
        return artifacts

    monkeypatch.setattr(coordination_module, "artifact_store", recording_artifact_store)

    coordinate_report(
        report_execution_input().model_dump(mode="json"),
        report_run_context().model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=report_run_context().modal_application,
        coordinator_invocation_id=COORDINATOR_CALL_ID,
        store=FakeStore(),
        invoker=RecordingInvoker(artifacts),
        aggregator=economy_aggregate,
        report_runs=FakeReportRuns(),
    )

    assert built == [True]
