"""A report run's workers store their own simulation's plan; the report reads its own.

Each worker is given its simulation's stored plan (``resolve_simulation_output_plan``),
the same whatever report names the run, so a shared run's file holds what any
report reads from that side. The report keeps the plan of its pair, which says
what the aggregation reads; a side that doesn't store the labour-supply
responses the report reads, because its own policy turns none on, is read with
those responses as zeros.
"""

from __future__ import annotations

from hashlib import sha256

import pytest
from policyengine_simulation_contract.stage12_execution import (
    EntityOutputPlan,
    SimulationRole,
    Stage12OutputPlan,
)
from policyengine_simulation_executor import spm as runtime_spm
from policyengine_simulation_executor.stage12_artifacts import (
    serialize_simulation_frames,
)
from policyengine_stage12_persistence import RunStatus
from stage12_fake_report_runs import (
    BASELINE_RUN_ID,
    REFORM_RUN_ID,
    REPORT_RUN_ID,
    ComparisonStoreNotUsed,
    FakeReportRuns,
    economy_aggregate,
    report_run_context,
    report_run_input,
)
from stage12_fake_stores import FakeArtifacts, simulation_frames
from stage12_run_worker_support import RunWorkerInvoker
from stage12_runtime_support import coordinate_report, shared_output_plan
from stage12_shared_run_support import StoredOutputFiles, stored_output_uri

COORDINATOR_CALL_ID = "fc-coordinator"
# The US labour-supply responses .py's output reads from both sides.
US_RESPONSES = (
    "income_elasticity_lsr",
    "substitution_elasticity_lsr",
    "weekly_hours_worked_behavioural_response_income_elasticity",
    "weekly_hours_worked_behavioural_response_substitution_elasticity",
)


@pytest.fixture(autouse=True)
def _no_spm_measure(monkeypatch):
    """Resolve every simulation to no SPM measure, so a stored file needs no receipt."""
    monkeypatch.setattr(runtime_spm, "normalize_runtime_spm", lambda _params: None)


def _with_person_columns(plan: Stage12OutputPlan, *, add=(), remove=(), active=None):
    """Return ``plan`` with its person columns changed, and labour supply as given."""
    entities = tuple(
        EntityOutputPlan(
            entity=entity.entity,
            materialized_variables=tuple(
                sorted((set(entity.materialized_variables) | set(add)) - set(remove))
            ),
        )
        if entity.entity == "person"
        else entity
        for entity in plan.entities
    )
    requirements = plan.requirements
    if active is not None:
        requirements = requirements.model_copy(
            update={"labor_supply_response_active": active}
        )
    return plan.model_copy(update={"entities": entities, "requirements": requirements})


def _coordinate(report_runs, invoker, artifacts, files, **seams):
    context = report_run_context()
    return coordinate_report(
        report_run_input().model_dump(mode="json"),
        context.model_dump(mode="json"),
        application_name=context.modal_application,
        coordinator_invocation_id=COORDINATOR_CALL_ID,
        store=ComparisonStoreNotUsed(),
        artifacts=artifacts,
        invoker=invoker,
        aggregator=seams.pop("aggregator", economy_aggregate),
        report_runs=report_runs,
        output_files=files,
        **seams,
    )


def test_each_worker_is_given_its_own_simulations_stored_plan() -> None:
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    invoker = RunWorkerInvoker(report_runs, artifacts)
    stored_plans = {
        SimulationRole.BASELINE: shared_output_plan(),
        SimulationRole.REFORM: _with_person_columns(
            shared_output_plan(), remove=("age",)
        ),
    }

    result = _coordinate(
        report_runs,
        invoker,
        artifacts,
        files,
        stored_plan_resolver=lambda simulation: stored_plans[simulation.role],
    )

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    planned = {
        simulation["role"]: Stage12OutputPlan.model_validate(simulation["output_plan"])
        for simulation in invoker.simulations
    }
    assert planned == {
        role.value: stored_plan for role, stored_plan in stored_plans.items()
    }


def _succeeded_with_frames(report_runs, files, run_id, frames) -> None:
    payload, _row_identity = serialize_simulation_frames(frames)
    uri = stored_output_uri(run_id)
    files.payloads[uri] = payload
    run = report_runs.simulation_runs[run_id]
    run.status = RunStatus.SUCCEEDED
    run.output_uri, run.output_sha256 = uri, sha256(payload).hexdigest()


def test_a_side_storing_no_labour_supply_responses_is_read_with_zeros() -> None:
    report_runs, files, artifacts = (
        FakeReportRuns(),
        StoredOutputFiles(),
        FakeArtifacts(),
    )
    # The report's reform turns responses on; its current-law baseline doesn't.
    read_plan = _with_person_columns(
        shared_output_plan(), add=US_RESPONSES, active=True
    )
    stored_plans = {
        SimulationRole.BASELINE: shared_output_plan(),
        SimulationRole.REFORM: read_plan,
    }
    baseline_frames = simulation_frames(100.0)
    reform_frames = simulation_frames(120.0)
    reform_frames["person"] = reform_frames["person"].assign(
        **dict.fromkeys(US_RESPONSES, 5.0)
    )
    _succeeded_with_frames(report_runs, files, BASELINE_RUN_ID, baseline_frames)
    _succeeded_with_frames(report_runs, files, REFORM_RUN_ID, reform_frames)
    aggregated = []

    def recording_aggregator(**inputs):
        aggregated.append(inputs)
        return economy_aggregate()

    result = _coordinate(
        report_runs,
        RunWorkerInvoker(report_runs, artifacts),
        artifacts,
        files,
        aggregator=recording_aggregator,
        output_plan_resolver=lambda _report: read_plan,
        stored_plan_resolver=lambda simulation: stored_plans[simulation.role],
    )

    assert result == {"report_run_id": str(REPORT_RUN_ID), "status": "succeeded"}
    (inputs,) = aggregated
    baseline_person = inputs["baseline"].frames["person"]
    reform_person = inputs["reform"].frames["person"]
    for column in US_RESPONSES:
        assert baseline_person[column].tolist() == [0.0]
        assert reform_person[column].tolist() == [5.0]
