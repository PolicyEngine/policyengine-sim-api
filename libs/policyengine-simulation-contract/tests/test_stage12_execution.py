"""Tests for typed Stage 12 execution contracts."""

from __future__ import annotations

import pytest

from policyengine_simulation_contract.stage12_execution import (
    EntityOutputPlan,
    ReportAggregate,
    ReportOutputRequirements,
    Stage12OutputPlan,
    stage12_output_plan_sha256,
)


def _plan() -> Stage12OutputPlan:
    return Stage12OutputPlan(
        country="us",
        requirements=ReportOutputRequirements(
            aggregates=tuple(ReportAggregate),
            include_cliff_impacts=False,
            labor_supply_response_active=False,
        ),
        entities=(
            EntityOutputPlan(
                entity="household",
                materialized_variables=("household_id", "household_net_income"),
                additional_variables=(),
            ),
            EntityOutputPlan(
                entity="person",
                materialized_variables=("federal_benefit_cost", "person_id"),
                additional_variables=("federal_benefit_cost",),
            ),
        ),
    )


def test_output_plan_has_a_deterministic_digest() -> None:
    plan = _plan()

    assert stage12_output_plan_sha256(plan) == stage12_output_plan_sha256(
        Stage12OutputPlan.model_validate(plan.model_dump(mode="json"))
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "materialized_variables",
            ("person_id", "federal_benefit_cost"),
            "canonical order",
        ),
        (
            "materialized_variables",
            ("person_id", "person_id"),
            "unique",
        ),
        (
            "additional_variables",
            ("missing",),
            "subset",
        ),
        (
            "dataset_variables",
            ("missing",),
            "subset",
        ),
    ],
)
def test_entity_output_plan_rejects_noncanonical_variables(
    field: str,
    value: tuple[str, ...],
    message: str,
) -> None:
    values = {
        "entity": "person",
        "materialized_variables": ("federal_benefit_cost", "person_id"),
        "additional_variables": ("federal_benefit_cost",),
        "dataset_variables": (),
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        EntityOutputPlan.model_validate(values)


def test_output_plan_rejects_duplicate_or_unsorted_entities() -> None:
    entity = _plan().entities[0]

    with pytest.raises(ValueError, match="canonical order"):
        Stage12OutputPlan(
            country="us",
            requirements=_plan().requirements,
            entities=(
                _plan().entities[1],
                entity,
            ),
        )

    with pytest.raises(ValueError, match="unique"):
        Stage12OutputPlan(
            country="us",
            requirements=_plan().requirements,
            entities=(entity, entity),
        )


def test_entity_output_plan_separates_calculated_and_dataset_variables() -> None:
    with pytest.raises(ValueError, match="must be disjoint"):
        EntityOutputPlan(
            entity="household",
            materialized_variables=("constituency_code_oa", "household_id"),
            additional_variables=("constituency_code_oa",),
            dataset_variables=("constituency_code_oa",),
        )
