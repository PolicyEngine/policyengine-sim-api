"""A year a coordinator starts runs as any report run does.

The year starter spawns this application's ``coordinate_report`` with an
input and context derived from the starting year's. The call must bind to
the coordinator function's signature, and the coordinator must claim the
started year's report run and calculate it, as it does a report run the
simulation service starts.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from uuid import UUID

from policyengine_simulation_contract.stage12_execution import ReportRunIds
from policyengine_simulation_executor.stage12_runtime import budget_window_scheduling
from policyengine_simulation_executor.stage12_runtime.budget_window_scheduling import (
    ModalYearStarter,
)
from policyengine_stage12_persistence import RunStatus, YearToStart
from stage12_fake_report_runs import (
    ComparisonStoreNotUsed,
    FakeReportRuns,
    FakeRun,
    economy_aggregate,
    report_run_context,
    report_run_input,
)
from stage12_fake_stores import FakeArtifacts
from stage12_run_worker_support import RunWorkerInvoker
from stage12_runtime_support import coordinate_report
from test_stage12_modal_app import _load

NEXT_YEAR_RUNS = ReportRunIds(
    report_run_id=UUID("00000000-0000-0000-0000-0000000002a1"),
    baseline_simulation_run_id=UUID("00000000-0000-0000-0000-0000000002b1"),
    reform_simulation_run_id=UUID("00000000-0000-0000-0000-0000000002c1"),
)
NEXT_YEAR_CALL_ID = "fc-next-year"


def _started_call(monkeypatch) -> tuple[tuple, dict]:
    """Start the next year from the shared report run; return the spawn's arguments."""
    spawned = []

    def from_name(*_args, **_kwargs):
        def spawn(*args, **kwargs):
            spawned.append((args, kwargs))
            return SimpleNamespace(object_id=NEXT_YEAR_CALL_ID)

        return SimpleNamespace(spawn=spawn)

    fake_modal = SimpleNamespace(Function=SimpleNamespace(from_name=from_name))
    monkeypatch.setattr(budget_window_scheduling, "import_module", lambda _: fake_modal)
    starter = ModalYearStarter(
        payload=report_run_input().model_dump(mode="json"),
        context=report_run_context().model_dump(mode="json"),
    )
    starter(YearToStart(year=2027, run_ids=NEXT_YEAR_RUNS))
    (call,) = spawned
    return call


def test_the_started_call_binds_to_the_coordinator_function(monkeypatch) -> None:
    args, kwargs = _started_call(monkeypatch)
    module = _load(monkeypatch)

    bound = inspect.signature(module.coordinate_report).bind(*args, **kwargs)

    # A report run's call passes no evaluation record.
    assert "parent" not in bound.arguments
    assert bound.arguments["context"]["report_run"] == NEXT_YEAR_RUNS.model_dump(
        mode="json"
    )


def test_the_coordinator_claims_and_calculates_the_started_year(monkeypatch) -> None:
    (payload, context), _ = _started_call(monkeypatch)
    report_runs = FakeReportRuns()
    report_runs.report_runs[NEXT_YEAR_RUNS.report_run_id] = FakeRun()
    for simulation_run_id in (
        NEXT_YEAR_RUNS.baseline_simulation_run_id,
        NEXT_YEAR_RUNS.reform_simulation_run_id,
    ):
        report_runs.simulation_runs[simulation_run_id] = FakeRun(year=2027)
    report_runs.simulation_run_pairs[NEXT_YEAR_RUNS.report_run_id] = (
        NEXT_YEAR_RUNS.baseline_simulation_run_id,
        NEXT_YEAR_RUNS.reform_simulation_run_id,
    )
    artifacts = FakeArtifacts()

    result = coordinate_report(
        payload,
        context,
        application_name=report_run_context().modal_application,
        coordinator_invocation_id=NEXT_YEAR_CALL_ID,
        store=ComparisonStoreNotUsed(),
        artifacts=artifacts,
        invoker=RunWorkerInvoker(report_runs, artifacts),
        aggregator=economy_aggregate,
        report_runs=report_runs,
    )

    assert result == {
        "report_run_id": str(NEXT_YEAR_RUNS.report_run_id),
        "status": "succeeded",
    }
    year_run = report_runs.report_runs[NEXT_YEAR_RUNS.report_run_id]
    assert (year_run.status, year_run.modal_call_id) == (
        RunStatus.SUCCEEDED,
        NEXT_YEAR_CALL_ID,
    )
