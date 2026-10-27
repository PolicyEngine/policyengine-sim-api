"""One v2 economy report run's comparison call: read both outputs, compare them."""

from __future__ import annotations

from hashlib import sha256
from uuid import UUID

import pandas as pd
import pytest
from fixtures.fake_object_store import InMemoryObjectStore
from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from policyengine_simulation_contract.economy_runs import (
    EconomyComparisonCall,
    EconomyComparisonSide,
)
from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_contract.report_execution import ReportExecutionError
from policyengine_simulation_contract.stage12_execution import (
    AggregateReportArtifactPayload,
    EntityOutputPlan,
    ReportAggregate,
    ReportOutputRequirements,
    SimulationExecutionInput,
    SimulationRole,
    Stage12OutputPlan,
)
from policyengine_simulation_executor.economy_runs import comparison_call
from policyengine_simulation_executor.economy_runs.comparison_call import (
    EconomyComparisonDependencies,
    compare_economy_simulations,
)
from policyengine_simulation_executor.stage12_artifacts import (
    Stage12ArtifactStore,
    serialize_simulation_frames,
)
from test_economy_simulation_call import STATES, _installed_bundle

REPORT_RUN_ID = UUID("00000000-0000-0000-0000-0000000000c1")
BASELINE_RUN_ID = UUID("00000000-0000-0000-0000-0000000000b1")
REFORM_RUN_ID = UUID("00000000-0000-0000-0000-0000000000b2")
REFORM_POLICY = {"gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01": 3000}}
LABOR_SUPPLY_POLICY = {
    "gov.simulation.labor_supply_responses.elasticities.income": {"2026-01-01": 0.1}
}


def _frames(value: float, *, labor_supply: bool = False) -> dict[str, pd.DataFrame]:
    household = {"household_id": [1, 2], "household_net_income": [value, value]}
    if labor_supply:
        household["income_elasticity_lsr"] = [0.0, 0.0]
    return {"household": pd.DataFrame(household)}


def _plan(_simulation: SimulationExecutionInput, *, labor_supply_response: bool):
    columns = ["household_id", "household_net_income"]
    if labor_supply_response:
        columns.append("income_elasticity_lsr")
    return Stage12OutputPlan(
        country="us",
        requirements=ReportOutputRequirements(
            aggregates=tuple(ReportAggregate),
            include_cliff_impacts=False,
            labor_supply_response_active=labor_supply_response,
        ),
        entities=(
            EntityOutputPlan(entity="household", materialized_variables=tuple(columns)),
        ),
    )


class _Objects:
    def __init__(self) -> None:
        self.objects = InMemoryObjectStore()
        self.artifacts = Stage12ArtifactStore("private", store=self.objects)

    def store(self, name: str, frames: dict[str, pd.DataFrame]) -> tuple[str, str]:
        payload, _identity = serialize_simulation_frames(frames)
        key = f"stage-12-runs/staging/2026/10/{name}.parquet"
        self.objects.upload_bytes(
            key, payload, content_type="application/vnd.apache.parquet"
        )
        return f"gs://private/{key}", sha256(payload).hexdigest()


def _side(
    objects: _Objects,
    run_id: UUID,
    frames: dict[str, pd.DataFrame],
    policy: dict,
) -> EconomyComparisonSide:
    uri, digest = objects.store(str(run_id), frames)
    return EconomyComparisonSide(
        simulation_run_id=run_id, policy=policy, output_uri=uri, output_sha256=digest
    )


def _call(
    baseline: EconomyComparisonSide, reform: EconomyComparisonSide, **changes
) -> EconomyComparisonCall:
    values = {
        "report_run_id": REPORT_RUN_ID,
        "country": "us",
        "year": 2026,
        "policyengine_version": "6.7.5",
        "region_code": "state/ca",
        "dataset_name": STATES.identity,
        "baseline": baseline,
        "reform": reform,
    }
    values.update(changes)
    return EconomyComparisonCall.model_validate(values)


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(comparison_call, "load_stage12_bundle", _installed_bundle)


class _Aggregator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *, report, baseline, reform, uk_local_authority_metadata):
        self.calls.append(
            {
                "report": report,
                "baseline": baseline,
                "reform": reform,
                "uk_local_authority_metadata": uk_local_authority_metadata,
            }
        )
        return AggregateReportArtifactPayload(
            evaluation_id=report.evaluation_id,
            requested_aggregates=report.requested_aggregates,
            bundle=report.baseline.bundle,
            result=CURRENT_SINGLE_YEAR_MACRO_RESULT,
        ).model_dump(mode="json")


def _compare(objects: _Objects, call: EconomyComparisonCall, aggregator=None):
    return compare_economy_simulations(
        call.model_dump(mode="json"),
        dependencies=EconomyComparisonDependencies(
            artifacts=objects.artifacts,
            aggregator=aggregator or _Aggregator(),
            output_planner=_plan,
        ),
    )


def test_a_comparison_aggregates_both_stored_outputs_into_the_result(
    installed,
) -> None:
    objects = _Objects()
    baseline = _side(objects, BASELINE_RUN_ID, _frames(100.0), {})
    reform = _side(objects, REFORM_RUN_ID, _frames(110.0), REFORM_POLICY)
    aggregator = _Aggregator()

    result = _compare(objects, _call(baseline, reform), aggregator)

    assert SingleYearMacroOutput.model_validate(result) == (
        SingleYearMacroOutput.model_validate(CURRENT_SINGLE_YEAR_MACRO_RESULT)
    )
    used = aggregator.calls[0]
    report = used["report"]
    assert report.evaluation_id == REPORT_RUN_ID
    assert report.baseline.role is SimulationRole.BASELINE
    assert report.reform.role is SimulationRole.REFORM
    assert report.baseline.policy == {}
    assert report.reform.policy == REFORM_POLICY
    assert report.baseline.geography.region == "state/ca"
    assert report.baseline.bundle.dataset.identity == STATES.identity
    assert used["baseline"].frames["household"]["household_net_income"].tolist() == [
        100.0,
        100.0,
    ]
    assert used["reform"].frames["household"]["household_net_income"].tolist() == [
        110.0,
        110.0,
    ]
    assert used["uk_local_authority_metadata"] is None


def test_a_run_compared_with_itself_reads_its_output_once_for_each_side(
    installed,
) -> None:
    objects = _Objects()
    side = _side(objects, BASELINE_RUN_ID, _frames(100.0), {})

    result = _compare(objects, _call(side, side))

    SingleYearMacroOutput.model_validate(result)


def test_a_stored_output_that_differs_from_its_digest_is_refused(installed) -> None:
    objects = _Objects()
    baseline = _side(objects, BASELINE_RUN_ID, _frames(100.0), {})
    reform = _side(objects, REFORM_RUN_ID, _frames(110.0), REFORM_POLICY)
    tampered = reform.model_copy(update={"output_sha256": "0" * 64})

    with pytest.raises(ReportExecutionError) as raised:
        _compare(objects, _call(baseline, tampered))

    assert raised.value.failure.evaluation_id == REPORT_RUN_ID


def test_outputs_of_different_households_are_not_compared(installed) -> None:
    objects = _Objects()
    baseline = _side(objects, BASELINE_RUN_ID, _frames(100.0), {})
    other = {
        "household": pd.DataFrame(
            {"household_id": [1, 3], "household_net_income": [1.0, 1.0]}
        )
    }
    reform = _side(objects, REFORM_RUN_ID, other, REFORM_POLICY)
    aggregator = _Aggregator()

    with pytest.raises(ReportExecutionError):
        _compare(objects, _call(baseline, reform), aggregator)

    assert aggregator.calls == []


def test_a_labour_supply_comparison_needs_both_outputs_columns(installed) -> None:
    objects = _Objects()
    baseline = _side(objects, BASELINE_RUN_ID, _frames(100.0), {})
    reform = _side(
        objects, REFORM_RUN_ID, _frames(110.0, labor_supply=True), LABOR_SUPPLY_POLICY
    )
    aggregator = _Aggregator()

    with pytest.raises(ReportExecutionError):
        _compare(objects, _call(baseline, reform), aggregator)

    assert aggregator.calls == []


def test_a_labour_supply_comparison_with_both_columns_is_compared(installed) -> None:
    objects = _Objects()
    baseline = _side(objects, BASELINE_RUN_ID, _frames(100.0, labor_supply=True), {})
    reform = _side(
        objects, REFORM_RUN_ID, _frames(110.0, labor_supply=True), LABOR_SUPPLY_POLICY
    )

    SingleYearMacroOutput.model_validate(_compare(objects, _call(baseline, reform)))


def test_a_comparison_for_another_policyengine_version_is_refused(installed) -> None:
    objects = _Objects()
    side = _side(objects, BASELINE_RUN_ID, _frames(100.0), {})

    with pytest.raises(ValueError, match=r"installs PolicyEngine\.py 6\.7\.5"):
        _compare(objects, _call(side, side, policyengine_version="6.8.0"))


def test_an_aggregate_that_describes_another_report_is_refused(installed) -> None:
    objects = _Objects()
    side = _side(objects, BASELINE_RUN_ID, _frames(100.0), {})

    def describe_another_report(
        *, report, baseline, reform, uk_local_authority_metadata
    ):
        del baseline, reform, uk_local_authority_metadata
        return AggregateReportArtifactPayload(
            evaluation_id=BASELINE_RUN_ID,
            requested_aggregates=report.requested_aggregates,
            bundle=report.baseline.bundle,
            result=CURRENT_SINGLE_YEAR_MACRO_RESULT,
        ).model_dump(mode="json")

    with pytest.raises(ReportExecutionError):
        _compare(objects, _call(side, side), describe_another_report)
