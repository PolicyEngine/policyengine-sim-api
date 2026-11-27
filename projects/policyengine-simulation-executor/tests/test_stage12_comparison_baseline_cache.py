"""A comparison-path report's current-law baseline finds the precomputed US baselines.

The precompute stores each US national baseline under its simulation's stored
plan (``resolve_simulation_output_plan``), and a segment worker looks its
baseline up under the digest of the plan its coordinator gave it. The
comparison-path coordinator (``ReportCoordination``), which serves shadow
comparisons and the operator route, gives its baseline that stored plan
whenever it holds every column the report reads from that side, so its
baseline segments load the precomputed files. A report reading columns the
stored plan lacks, such as the cliff columns, keeps the report's plan, and the
reform always keeps it.
"""

from __future__ import annotations

from uuid import UUID, uuid5

import pandas as pd
import pytest
from policyengine_simulation_contract.stage12_execution import (
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    EntityOutputPlan,
    GeographySelection,
    PlannedSimulationExecutionInput,
    ReportAggregate,
    ReportExecutionInput,
    SimulationExecutionInput,
    SimulationRole,
    Stage12OutputPlan,
    stage12_output_plan_sha256,
)
from policyengine_simulation_executor import simulation_runtime
from policyengine_simulation_executor.spm import normalize_runtime_spm
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle
from policyengine_simulation_executor.stage12_cache import precompute_planning
from policyengine_simulation_executor.stage12_runtime import segmentation
from policyengine_simulation_executor.stage12_runtime.cached_simulation import (
    qualifying_cache_identity,
)
from policyengine_simulation_executor.stage12_runtime.coordination_core import (
    CoordinatorDependencies,
    CoordinatorInvocation,
)
from policyengine_simulation_executor.stage12_runtime.failure_causes import (
    StageProgress,
)
from policyengine_simulation_executor.stage12_runtime.output_planning import (
    resolve_simulation_output_plan,
)
from policyengine_simulation_executor.stage12_runtime.partition import (
    US_REGION_GROUPS,
    should_segment_simulation,
)
from policyengine_simulation_executor.stage12_runtime.report_coordination import (
    ReportCoordination,
)
from policyengine_simulation_executor.stage12_runtime.worker_inputs import (
    simulation_runtime_params,
)
from policyengine_simulation_observability.stages import (
    STAGE12_CANONICAL_REPORT_STAGES,
)
from stage12_child_call_support import ImmediateCall
from stage12_fake_stores import FakeArtifacts, FakeStore
from stage12_runtime_support import (
    aggregate_payload,
    coordinate_report,
    invocation_context,
    parent_record,
    report_execution_input,
    shared_output_plan,
)

REPORT_YEAR = 2026
# The reform of the 1.6 · 7 staging report: the CTC base amount raised to $3,000.
CTC_REFORM = {
    "gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01.2100-12-31": 3000}
}
# The same reform with an income-elasticity labour-supply response turned on.
CTC_REFORM_WITH_LABOUR_SUPPLY_RESPONSES = {
    **CTC_REFORM,
    "gov.simulation.labor_supply_responses.elasticities.income": {
        "2026-01-01.2100-12-31": -0.05
    },
}
REPORT_EVALUATION_ID = UUID("34d79a08-6c93-401a-a6da-0fc6580fe2ab")
# The US labour-supply responses .py's output reads from both sides.
US_RESPONSES = (
    "income_elasticity_lsr",
    "substitution_elasticity_lsr",
    "weekly_hours_worked_behavioural_response_income_elasticity",
    "weekly_hours_worked_behavioural_response_substitution_elasticity",
)
# The inputs a US stored plan copies for .py's labour-supply output.
US_COPIED_INPUTS = ("self_employment_income", "weekly_hours_worked")


def _national_report(
    reform_policy: dict, *, include_cliffs: bool = False
) -> ReportExecutionInput:
    """Build a US national report as the entrypoint's ``adapt_annual_comparison`` does.

    The baseline is current law and the population is the installed bundle's
    default dataset, with every aggregate requested.
    """
    resolved = load_stage12_bundle()
    country = next(item for item in resolved.bundle.countries if item.country == "us")
    dataset = next(
        item for item in country.datasets if item.identity == country.default_dataset
    )
    provenance = BundleProvenance(
        policyengine_version=resolved.bundle.policyengine_version,
        country_package_name=country.country_package_name,
        country_package_version=country.country_package_version,
        dataset=DatasetProvenance(
            identity=dataset.identity,
            uri=dataset.uri,
            artifact_revision=dataset.artifact_revision,
            data_package_name=country.data_package_name,
            data_package_version=country.data_package_version,
        ),
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
    )
    population = DatasetPopulationInput(
        artifact=DatasetArtifactReference(
            uri=dataset.uri,
            media_type=DatasetArtifactMediaType.HDF5,
            content_sha256=dataset.sha256,
        )
    )
    options = {"include_cliffs": True} if include_cliffs else {}

    def simulation(role: SimulationRole, policy: dict) -> SimulationExecutionInput:
        return SimulationExecutionInput(
            evaluation_id=REPORT_EVALUATION_ID,
            simulation_execution_id=uuid5(REPORT_EVALUATION_ID, role.value),
            role=role,
            policy=policy,
            population=population,
            year=REPORT_YEAR,
            geography=GeographySelection(country="us", region="us"),
            options=options,
            bundle=provenance,
        )

    return ReportExecutionInput(
        evaluation_id=REPORT_EVALUATION_ID,
        baseline=simulation(SimulationRole.BASELINE, {}),
        reform=simulation(SimulationRole.REFORM, reform_policy),
        requested_aggregates=tuple(ReportAggregate),
    )


def _planned_by_the_comparison_coordinator(
    report: ReportExecutionInput,
) -> tuple[Stage12OutputPlan, dict[SimulationRole, PlannedSimulationExecutionInput]]:
    """Plan ``report`` as ``ReportCoordination`` does, with the deployed planners.

    Returns the report's plan and each child's planned input by role. Planning
    reads only the installed packages: no store, artifact or child is used.
    """
    coordination = ReportCoordination(
        report=report,
        context=invocation_context(),
        invocation=CoordinatorInvocation(
            application_name=invocation_context().modal_application,
            invocation_id="coordinator-1",
        ),
        persistence=FakeStore(),
        artifact_storage=FakeArtifacts(),
        child_invoker=None,
        dependencies=CoordinatorDependencies(),
        stage_plan=STAGE12_CANONICAL_REPORT_STAGES,
        progress=StageProgress(),
        child_wait_deadline=0.0,
    )
    report_plan, children = coordination.plan_simulations()
    return report_plan, {child.role: child for child in children}


def _group_segment_identity(
    planned_baseline: PlannedSimulationExecutionInput, group_index: int
):
    """Return the cache identity a segment worker computes for one region group.

    The segment is scoped as ``segmentation`` scopes it, and its region and
    dataset are resolved as the worker resolves them, without loading data.
    """
    segment = segmentation.build_segment_inputs(planned_baseline)[group_index]
    scoped = segmentation._scoped_simulation(segment)
    params = simulation_runtime_params(scoped)
    region = simulation_runtime.resolve_region(
        country_module=simulation_runtime._country_module("us"),
        country="us",
        params=params,
    )
    dataset_selection = simulation_runtime._resolve_dataset_selection(
        params, region_resolution=region
    )
    return qualifying_cache_identity(
        scoped,
        resolved=load_stage12_bundle(),
        dataset_is_default=dataset_selection.is_default,
        scoping_strategy=region.scoping_strategy,
        spm=normalize_runtime_spm(params),
    )


def _precomputed_simulation_id(group_index: int) -> str:
    _simulation, identity = precompute_planning.baseline_identity(
        REPORT_YEAR, US_REGION_GROUPS[group_index]
    )
    return identity.simulation_id


def test_comparison_report_baseline_segment_uses_the_precomputed_identity() -> None:
    _report_plan, children = _planned_by_the_comparison_coordinator(
        _national_report(CTC_REFORM)
    )

    identity = _group_segment_identity(children[SimulationRole.BASELINE], 0)

    assert identity is not None
    assert identity.simulation_id == _precomputed_simulation_id(0)


def test_a_labour_supply_reform_splits_its_baseline_into_precomputed_segments() -> None:
    report_plan, children = _planned_by_the_comparison_coordinator(
        _national_report(CTC_REFORM_WITH_LABOUR_SUPPLY_RESPONSES)
    )
    baseline = children[SimulationRole.BASELINE]

    # The report reads responses; its current-law baseline stores none, and
    # its region-group segments are looked up in the precomputed cache.
    assert report_plan.requirements.labor_supply_response_active
    assert should_segment_simulation(baseline)
    identity = _group_segment_identity(baseline, 0)
    assert identity is not None
    assert identity.simulation_id == _precomputed_simulation_id(0)
    # The reform's own policy turns responses on, so it isn't split.
    assert not should_segment_simulation(children[SimulationRole.REFORM])


def test_a_cliff_report_keeps_the_report_plan_for_its_baseline() -> None:
    report = _national_report(CTC_REFORM, include_cliffs=True)
    report_plan, children = _planned_by_the_comparison_coordinator(report)

    # The stored plan never holds the cliff columns the report reads.
    stored_columns = {
        entity.entity: set(entity.materialized_variables)
        for entity in resolve_simulation_output_plan(report.baseline).entities
    }
    assert "cliff_gap" not in stored_columns["person"]
    assert children[SimulationRole.BASELINE].output_plan == report_plan
    assert not should_segment_simulation(children[SimulationRole.BASELINE])


@pytest.mark.parametrize(
    "reform_policy", [CTC_REFORM, CTC_REFORM_WITH_LABOUR_SUPPLY_RESPONSES]
)
def test_the_reform_keeps_the_report_plan(reform_policy: dict) -> None:
    report_plan, children = _planned_by_the_comparison_coordinator(
        _national_report(reform_policy)
    )

    assert children[SimulationRole.REFORM].output_plan == report_plan


def _with_person_columns(
    plan: Stage12OutputPlan,
    columns: tuple[str, ...],
    *,
    labour_supply_response_active: bool,
) -> Stage12OutputPlan:
    """Return ``plan`` with ``columns`` added to its person entity."""
    entities = tuple(
        EntityOutputPlan(
            entity=entity.entity,
            materialized_variables=tuple(
                sorted(set(entity.materialized_variables) | set(columns))
            ),
        )
        if entity.entity == "person"
        else entity
        for entity in plan.entities
    )
    requirements = plan.requirements.model_copy(
        update={"labor_supply_response_active": labour_supply_response_active}
    )
    return plan.model_copy(update={"entities": entities, "requirements": requirements})


class PlanShapedInvoker:
    """Start each child by writing a one-row output holding exactly its plan's columns.

    A response column is zero in the baseline, whose current-law policy turns
    no response on, and the copied inputs are the same on both sides.
    """

    def __init__(self, artifacts: FakeArtifacts) -> None:
        self.artifacts = artifacts
        self.planned: dict[str, Stage12OutputPlan] = {}

    def spawn(self, request):
        simulation = PlannedSimulationExecutionInput.model_validate(request.simulation)
        role = simulation.role.value
        self.planned[role] = simulation.output_plan
        frames = {
            entity.entity: pd.DataFrame(
                {
                    column: [_column_value(column, simulation.role)]
                    for column in entity.materialized_variables
                }
            )
            for entity in simulation.output_plan.entities
        }
        descriptor = self.artifacts.add_simulation(simulation, frames).model_copy(
            update={
                "output_plan_sha256": stage12_output_plan_sha256(simulation.output_plan)
            }
        )
        return ImmediateCall(f"call-{role}", descriptor.model_dump(mode="json"), [])

    def restore(self, invocation_id):
        raise AssertionError(invocation_id)


def _column_value(column: str, role: SimulationRole) -> float | int:
    if column.endswith("_id"):
        return 1
    if column in US_COPIED_INPUTS:
        return 7.0
    if column in US_RESPONSES:
        return 0.0 if role is SimulationRole.BASELINE else 5.0
    return 100.0 if role is SimulationRole.BASELINE else 120.0


def _aggregated_frames(report_plan, baseline_stored_plan):
    """Coordinate the fixture report; return the plans given and frames aggregated."""
    artifacts = FakeArtifacts()
    invoker = PlanShapedInvoker(artifacts)
    aggregated = []

    def recording_aggregator(**inputs):
        aggregated.append(inputs)
        return aggregate_payload()

    coordinate_report(
        report_execution_input().model_dump(mode="json"),
        invocation_context().model_dump(mode="json"),
        parent_record().model_dump(mode="json"),
        application_name=invocation_context().modal_application,
        coordinator_invocation_id="coordinator-1",
        store=FakeStore(),
        artifacts=artifacts,
        invoker=invoker,
        aggregator=recording_aggregator,
        output_plan_resolver=lambda _report: report_plan,
        stored_plan_resolver=lambda _simulation: baseline_stored_plan,
    )
    (inputs,) = aggregated
    return invoker.planned, {
        role: inputs[role].frames for role in ("baseline", "reform")
    }


def _report_columns(frames, report_plan):
    """Return each entity's frame cut down to the columns the report reads."""
    return {
        entity.entity: frames[entity.entity][list(entity.materialized_variables)]
        for entity in report_plan.entities
    }


def _labour_supply_report_plans() -> tuple[Stage12OutputPlan, Stage12OutputPlan]:
    """Return the plans of a report whose reform turns responses on.

    The report reads the responses and the copied inputs; the current-law
    baseline's stored plan holds the copied inputs and no response.
    """
    return (
        _with_person_columns(
            shared_output_plan(),
            US_RESPONSES + US_COPIED_INPUTS,
            labour_supply_response_active=True,
        ),
        _with_person_columns(
            shared_output_plan(), US_COPIED_INPUTS, labour_supply_response_active=False
        ),
    )


def _no_response_report_plans() -> tuple[Stage12OutputPlan, Stage12OutputPlan]:
    """Return the plans of a report turning no response on.

    The baseline's stored plan holds the copied inputs, which neither side of
    the report reads.
    """
    return (
        shared_output_plan(),
        _with_person_columns(
            shared_output_plan(), US_COPIED_INPUTS, labour_supply_response_active=False
        ),
    )


@pytest.mark.parametrize(
    "report_plans",
    [_labour_supply_report_plans, _no_response_report_plans],
    ids=["reform_turns_responses_on", "no_responses"],
)
def test_a_baseline_stored_under_its_own_plan_gives_the_same_report_columns(
    report_plans,
):
    # The aggregation reads each side's columns by name, so a side whose
    # report-plan columns hold the same values aggregates to the same result,
    # whatever other columns its file holds.
    report_plan, baseline_stored_plan = report_plans()

    stored_planned, stored_frames = _aggregated_frames(
        report_plan, baseline_stored_plan
    )
    # A stored plan equal to the report's plan stands for the earlier planning,
    # where the baseline calculated the report's plan, responses as zeros.
    report_planned, report_frames = _aggregated_frames(report_plan, report_plan)

    assert stored_planned == {"baseline": baseline_stored_plan, "reform": report_plan}
    assert report_planned == {"baseline": report_plan, "reform": report_plan}
    for role in ("baseline", "reform"):
        stored_side = _report_columns(stored_frames[role], report_plan)
        report_side = _report_columns(report_frames[role], report_plan)
        for entity, frame in report_side.items():
            pd.testing.assert_frame_equal(stored_side[entity], frame)
