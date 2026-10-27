"""A v2 economy simulation run's output columns serve any report it is in."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from policyengine_simulation_contract.stage12_execution import ReportAggregate
from policyengine_simulation_executor.stage12_runtime.output_planning import (
    resolve_report_output_plan,
    resolve_simulation_output_plan,
)
from test_stage12_output_planning import _report

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_execution import Stage12OutputPlan

LABOR_SUPPLY_REFORM = {"gov.simulation.labor_supply_responses.elasticities.income": 0.1}
UNRELATED_REFORM = {"gov.irs.credits.ctc.amount.base[0].amount": 3_000}


def _columns(plan: Stage12OutputPlan) -> dict[str, tuple[tuple[str, ...], ...]]:
    return {
        entity.entity: (
            entity.materialized_variables,
            entity.additional_variables,
            entity.dataset_variables,
        )
        for entity in plan.entities
    }


@pytest.mark.parametrize("country", ["us", "uk"])
def test_a_simulation_plans_the_columns_a_report_without_labour_supply_plans(
    country,
) -> None:
    report = _report(country)

    plan = resolve_simulation_output_plan(report.baseline, labor_supply_response=False)

    assert _columns(plan) == _columns(resolve_report_output_plan(report))
    assert plan.requirements.aggregates == tuple(ReportAggregate)
    assert plan.requirements.include_cliff_impacts is False
    assert plan.requirements.labor_supply_response_active is False


def test_a_current_law_simulation_plans_the_labour_supply_columns_on_request() -> None:
    report = _report("us", reform=LABOR_SUPPLY_REFORM)

    plan = resolve_simulation_output_plan(report.baseline, labor_supply_response=True)

    assert _columns(plan) == _columns(resolve_report_output_plan(report))
    assert plan.requirements.labor_supply_response_active is True


def test_a_reform_plans_the_same_columns_as_current_law() -> None:
    report = _report("us", reform=UNRELATED_REFORM)

    baseline = resolve_simulation_output_plan(
        report.baseline, labor_supply_response=False
    )
    reform = resolve_simulation_output_plan(report.reform, labor_supply_response=False)

    assert baseline == reform


@pytest.mark.parametrize("region", ["constituency/E14001063", "country/wales"])
def test_a_uk_area_simulation_plans_the_area_codes_its_report_reads(region) -> None:
    report = _report("uk", region=region)

    plan = resolve_simulation_output_plan(report.baseline, labor_supply_response=False)

    assert _columns(plan) == _columns(resolve_report_output_plan(report))
