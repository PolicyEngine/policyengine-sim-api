"""Tests for country-aware Stage 12 output planning."""

from __future__ import annotations

from uuid import UUID

import pandas as pd
import pytest
from policyengine.core import Simulation
from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_contract.stage12_execution import (
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    GeographySelection,
    ReportAggregate,
    ReportExecutionInput,
    SimulationExecutionInput,
    SimulationRole,
    Stage12OutputPlan,
)
from pydantic import JsonValue

from policyengine_simulation_executor.stage12_runtime.output_planning import (
    apply_output_plan,
    resolve_report_output_plan,
    validate_output_frames,
)

EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000001")


def _bundle(country: CountryId) -> BundleProvenance:
    package_name, package_version, dataset = (
        ("policyengine-us", "1.764.6", "populace_us_2024")
        if country == "us"
        else ("policyengine-uk", "2.90.2", "populace_uk_2023")
    )
    return BundleProvenance(
        policyengine_version="5.2.0",
        country_package_name=package_name,
        country_package_version=package_version,
        dataset=DatasetProvenance(
            identity=dataset,
            uri=f"hf://policyengine/data/{dataset}.h5@revision",
            artifact_revision="revision",
            data_package_name="populace-data",
            data_package_version="0.1.0",
        ),
        bundle_manifest_sha256="b" * 64,
    )


def _simulation(
    country: CountryId,
    role: SimulationRole,
    *,
    policy: dict[str, JsonValue] | None = None,
    options: dict[str, JsonValue] | None = None,
) -> SimulationExecutionInput:
    bundle = _bundle(country)
    return SimulationExecutionInput(
        evaluation_id=EVALUATION_ID,
        simulation_execution_id=UUID(
            "00000000-0000-0000-0000-000000000002"
            if role is SimulationRole.BASELINE
            else "00000000-0000-0000-0000-000000000003"
        ),
        role=role,
        policy=policy or {},
        population=DatasetPopulationInput(
            artifact=DatasetArtifactReference(
                uri=bundle.dataset.uri,
                media_type=DatasetArtifactMediaType.HDF5,
                content_sha256="a" * 64,
            )
        ),
        year=2026,
        geography=GeographySelection(country=country, region=country),
        options=options or {},
        bundle=bundle,
    )


def _report(
    country: CountryId = "us",
    *,
    reform: dict[str, JsonValue] | None = None,
    include_cliffs: bool = False,
    aggregates: tuple[ReportAggregate, ...] = tuple(ReportAggregate),
) -> ReportExecutionInput:
    options: dict[str, JsonValue] = {"include_cliffs": True} if include_cliffs else {}
    return ReportExecutionInput(
        evaluation_id=EVALUATION_ID,
        baseline=_simulation(
            country,
            SimulationRole.BASELINE,
            options=options,
        ),
        reform=_simulation(
            country,
            SimulationRole.REFORM,
            policy=reform,
            options=options,
        ),
        requested_aggregates=aggregates,
    )


def _variables(plan: Stage12OutputPlan, entity: str) -> set[str]:
    entity_plan = next(item for item in plan.entities if item.entity == entity)
    return set(entity_plan.materialized_variables)


def _additional_variables(plan: Stage12OutputPlan, entity: str) -> set[str]:
    entity_plan = next(item for item in plan.entities if item.entity == entity)
    return set(entity_plan.additional_variables)


def test_us_plan_adds_budget_variables_to_the_country_defaults() -> None:
    plan = resolve_report_output_plan(_report())

    person_variables = _variables(plan, "person")
    assert {"person_id", "age"}.issubset(person_variables)
    assert {"federal_benefit_cost", "state_benefit_cost"}.issubset(person_variables)
    assert {"federal_benefit_cost", "state_benefit_cost"}.issubset(
        _additional_variables(plan, "person")
    )


def test_uk_plan_uses_uk_defaults_without_us_budget_variables() -> None:
    plan = resolve_report_output_plan(_report("uk"))

    assert "benunit" in {entity.entity for entity in plan.entities}
    assert "tax_unit" not in {entity.entity for entity in plan.entities}
    assert "federal_benefit_cost" not in _variables(plan, "person")


def test_cliff_variables_are_conditional() -> None:
    without_cliffs = resolve_report_output_plan(_report())
    with_cliffs = resolve_report_output_plan(_report(include_cliffs=True))

    assert with_cliffs.requirements.include_cliff_impacts is True
    assert {"cliff_gap", "is_on_cliff", "is_adult"}.issubset(
        _additional_variables(with_cliffs, "person")
    )
    assert "cliff_gap" not in _additional_variables(without_cliffs, "person")


def test_labor_supply_variables_are_shared_when_either_policy_activates_them() -> None:
    plan = resolve_report_output_plan(
        _report(
            reform={"gov.simulation.labor_supply_responses.elasticities.income": 0.1}
        )
    )

    assert plan.requirements.labor_supply_response_active is True
    assert {
        "income_elasticity_lsr",
        "substitution_elasticity_lsr",
        "weekly_hours_worked_behavioural_response_income_elasticity",
        "weekly_hours_worked_behavioural_response_substitution_elasticity",
    }.issubset(_additional_variables(plan, "person"))


def test_unrelated_policy_does_not_activate_labor_supply_outputs() -> None:
    plan = resolve_report_output_plan(
        _report(reform={"gov.irs.credits.ctc.amount.base[0].amount": 3_000})
    )

    assert plan.requirements.labor_supply_response_active is False
    assert "income_elasticity_lsr" not in _additional_variables(plan, "person")


def test_planning_is_deterministic_and_never_ensures_a_simulation(monkeypatch) -> None:
    def reject_ensure(_simulation):
        raise AssertionError("output planning must not execute a simulation")

    monkeypatch.setattr(Simulation, "ensure", reject_ensure)

    first = resolve_report_output_plan(_report(include_cliffs=True))
    second = resolve_report_output_plan(_report(include_cliffs=True))

    assert first == second


def test_incomplete_aggregate_profile_is_rejected_before_dispatch() -> None:
    with pytest.raises(ValueError, match="complete aggregate profile"):
        resolve_report_output_plan(_report(aggregates=(ReportAggregate.BUDGET,)))


def test_apply_and_validate_output_plan_reject_missing_materialized_columns() -> None:
    from policyengine.tax_benefit_models import us

    plan = resolve_report_output_plan(_report())
    simulation = Simulation.model_construct(
        policy={},
        dynamic=None,
        dataset=None,
        scoping_strategy=None,
        extra_variables={},
        tax_benefit_model_version=us.model,
        output_dataset=None,
    )

    apply_output_plan(simulation, plan)
    resolved = us.model.resolve_entity_variables(simulation)
    person_plan = next(entity for entity in plan.entities if entity.entity == "person")
    assert set(person_plan.additional_variables).issubset(
        set(resolved[person_plan.entity])
    )

    frames = {
        entity.entity: pd.DataFrame(
            {variable: [0] for variable in entity.materialized_variables}
        )
        for entity in plan.entities
    }
    validate_output_frames(frames, plan)
    frames["person"] = frames["person"].drop(columns=["federal_benefit_cost"])
    with pytest.raises(ValueError, match="federal_benefit_cost"):
        validate_output_frames(frames, plan)
