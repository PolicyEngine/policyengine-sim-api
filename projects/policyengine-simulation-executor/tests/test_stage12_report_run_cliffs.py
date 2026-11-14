"""A report run that asks for cliff impacts is refused until cliffs return.

Shared simulation runs never store the cliff columns (C1 · 24, answer 4), and
cliffs are to come back as a calculation of each simulation run in a later
stage. Until then the coordinator fails such a report run before any worker
starts, with its own error code, and touches none of its simulation runs, so
none is marked as one a report run refused to reuse.
"""

from __future__ import annotations

import pytest
from policyengine_stage12_persistence import RunStatus
from stage12_fake_report_runs import (
    REPORT_RUN_ID,
    ComparisonStoreNotUsed,
    FakeReportRuns,
    economy_aggregate,
    report_run_context,
    report_run_input,
)
from stage12_fake_stores import FakeArtifacts
from stage12_run_worker_support import RunWorkerInvoker
from stage12_runtime_support import coordinate_report

COORDINATOR_CALL_ID = "fc-coordinator"


def _report_asking_for_cliffs():
    report = report_run_input()
    options = {**report.baseline.options, "include_cliffs": True}
    return report.model_copy(
        update={
            "baseline": report.baseline.model_copy(update={"options": options}),
            "reform": report.reform.model_copy(update={"options": options}),
        }
    )


def test_a_report_run_asking_for_cliffs_is_refused_before_any_worker() -> None:
    report_runs, artifacts = FakeReportRuns(), FakeArtifacts()
    invoker = RunWorkerInvoker(report_runs, artifacts)
    context = report_run_context()

    with pytest.raises(RuntimeError, match="coordination failed"):
        coordinate_report(
            _report_asking_for_cliffs().model_dump(mode="json"),
            context.model_dump(mode="json"),
            application_name=context.modal_application,
            coordinator_invocation_id=COORDINATOR_CALL_ID,
            store=ComparisonStoreNotUsed(),
            artifacts=artifacts,
            invoker=invoker,
            aggregator=economy_aggregate,
            report_runs=report_runs,
        )

    report_run = report_runs.report_runs[REPORT_RUN_ID]
    assert report_run.status is RunStatus.FAILED
    assert report_run.error_message.startswith("cliff_impacts_unavailable: ")
    assert invoker.events == []
    assert all(
        (simulation_run.status, simulation_run.writes) == (RunStatus.PENDING, [])
        for simulation_run in report_runs.simulation_runs.values()
    )
