"""Tests for country-aware Stage 12 output planning."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import UUID

import pandas as pd
import pytest
from policyengine.core import Simulation
from policyengine_simulation_contract.stage12_execution import (
    LOCAL_AREA_AGGREGATES,
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
from policyengine_simulation_executor.stage12_runtime.output_planning import (
    apply_output_plan,
    resolve_local_area_output_plan,
    resolve_report_output_plan,
    resolve_simulation_output_plan,
    validate_output_frames,
)
from policyengine_simulation_observability.stages import Stage

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_bundle import CountryId
    from pydantic import JsonValue

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
    region: str | None = None,
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
        geography=GeographySelection(country=country, region=region or country),
        options=options or {},
        bundle=bundle,
    )


def _report(
    country: CountryId = "us",
    *,
    reform: dict[str, JsonValue] | None = None,
    include_cliffs: bool = False,
    aggregates: tuple[ReportAggregate, ...] = tuple(ReportAggregate),
    region: str | None = None,
) -> ReportExecutionInput:
    options: dict[str, JsonValue] = {"include_cliffs": True} if include_cliffs else {}
    return ReportExecutionInput(
        evaluation_id=EVALUATION_ID,
        baseline=_simulation(
            country,
            SimulationRole.BASELINE,
            options=options,
            region=region,
        ),
        reform=_simulation(
            country,
            SimulationRole.REFORM,
            policy=reform,
            options=options,
            region=region,
        ),
        requested_aggregates=aggregates,
    )


def _variables(plan: Stage12OutputPlan, entity: str) -> set[str]:
    entity_plan = next(item for item in plan.entities if item.entity == entity)
    return set(entity_plan.materialized_variables)


def _additional_variables(plan: Stage12OutputPlan, entity: str) -> set[str]:
    entity_plan = next(item for item in plan.entities if item.entity == entity)
    return set(entity_plan.additional_variables)


def _dataset_variables(plan: Stage12OutputPlan, entity: str) -> set[str]:
    entity_plan = next(item for item in plan.entities if item.entity == entity)
    return set(entity_plan.dataset_variables)


def test_us_plan_adds_budget_variables_to_the_country_defaults() -> None:
    plan = resolve_report_output_plan(_report())

    person_variables = _variables(plan, "person")
    assert {"person_id", "age"}.issubset(person_variables)
    assert {"federal_benefit_cost", "state_benefit_cost"}.issubset(person_variables)
    assert {"federal_benefit_cost", "state_benefit_cost"}.issubset(
        _additional_variables(plan, "person")
    )


def _route_uk_area_regions(monkeypatch, *, routed: bool) -> None:
    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "uk_area_regions_routed",
        lambda: routed,
    )


def test_uk_plan_uses_uk_defaults_without_us_budget_variables(monkeypatch) -> None:
    _route_uk_area_regions(monkeypatch, routed=False)
    plan = resolve_report_output_plan(_report("uk"))

    assert "benunit" in {entity.entity for entity in plan.entities}
    assert "tax_unit" not in {entity.entity for entity in plan.entities}
    assert "federal_benefit_cost" not in _variables(plan, "person")
    assert _dataset_variables(plan, "household") == {
        "constituency_code_oa",
        "la_code_oa",
        "region_code_oa",
        "source_household_id",
    }
    assert _dataset_variables(plan, "household").issubset(_variables(plan, "household"))


@pytest.mark.parametrize(
    ("region", "routed", "planned"),
    [
        ("uk", False, True),
        ("uk", True, False),
        ("country/wales", True, False),
        ("constituency/E14001063", True, True),
        ("local_authority/E06000063", True, True),
    ],
)
def test_uk_plan_copies_area_codes_unless_a_routed_national_report_lacks_them(
    monkeypatch, region: str, *, routed: bool, planned: bool
) -> None:
    _route_uk_area_regions(monkeypatch, routed=routed)

    plan = resolve_report_output_plan(_report("uk", region=region))

    assert _dataset_variables(plan, "household") == (
        {
            "constituency_code_oa",
            "la_code_oa",
            "region_code_oa",
            "source_household_id",
        }
        if planned
        else set()
    )


def test_uk_geographic_plan_copies_the_routed_dataset_columns(monkeypatch) -> None:
    from policyengine_simulation_executor import uk_local_authority_metadata

    _route_uk_area_regions(monkeypatch, routed=True)

    plan = resolve_report_output_plan(_report("uk", region="constituency/E14001063"))

    # Stage 12 copies exactly the columns a routed area dataset must carry.
    household = next(item for item in plan.entities if item.entity == "household")
    assert tuple(household.dataset_variables) == (
        uk_local_authority_metadata.UK_AREA_DATASET_COLUMNS
    )


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
        msg = "output planning must not execute a simulation"
        raise AssertionError(msg)

    monkeypatch.setattr(Simulation, "ensure", reject_ensure)

    first = resolve_report_output_plan(_report(include_cliffs=True))
    second = resolve_report_output_plan(_report(include_cliffs=True))

    assert first == second


def test_output_planning_records_each_expensive_substage() -> None:
    observed: list[tuple[Stage, dict[str, object]]] = []

    @contextmanager
    def record_stage(stage: Stage, attributes):
        observed.append((stage, dict(attributes or {})))
        yield

    resolve_report_output_plan(_report(), stage_scope=record_stage)

    assert observed == [
        (Stage.STAGE12_COUNTRY_MODEL_LOAD, {"country": "us"}),
        (Stage.STAGE12_OUTPUT_CONFIGURATION, {"country": "us"}),
        (Stage.STAGE12_OUTPUT_VARIABLE_RESOLUTION, {"country": "us"}),
    ]


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


@pytest.mark.parametrize(
    "missing",
    ["constituency_code_oa", "la_code_oa", "region_code_oa", "source_household_id"],
)
def test_uk_output_validation_requires_geographic_dataset_columns(
    monkeypatch,
    missing: str,
) -> None:
    from policyengine.tax_benefit_models import uk

    _route_uk_area_regions(monkeypatch, routed=False)
    plan = resolve_report_output_plan(_report("uk"))
    simulation = Simulation.model_construct(
        policy={},
        dynamic=None,
        dataset=None,
        scoping_strategy=None,
        extra_variables={},
        tax_benefit_model_version=uk.model,
        output_dataset=None,
    )

    # Dataset variables are copied by the country model rather than calculated,
    # so applying the plan must not try to add them to ``extra_variables``.
    apply_output_plan(simulation, plan)
    assert not _dataset_variables(plan, "household").intersection(
        simulation.extra_variables.get("household", ())
    )

    frames = {
        entity.entity: pd.DataFrame(
            {variable: [0] for variable in entity.materialized_variables}
        )
        for entity in plan.entities
    }
    validate_output_frames(frames, plan)
    frames["household"] = frames["household"].drop(columns=[missing])

    with pytest.raises(ValueError, match=missing):
        validate_output_frames(frames, plan)


def _self_comparison(simulation: SimulationExecutionInput) -> ReportExecutionInput:
    """Return the report that compares ``simulation`` with itself."""
    return ReportExecutionInput(
        evaluation_id=EVALUATION_ID,
        baseline=simulation.model_copy(
            update={
                "role": SimulationRole.BASELINE,
                "simulation_execution_id": UUID("00000000-0000-0000-0000-000000000002"),
            }
        ),
        reform=simulation.model_copy(
            update={
                "role": SimulationRole.REFORM,
                "simulation_execution_id": UUID("00000000-0000-0000-0000-000000000003"),
            }
        ),
        requested_aggregates=tuple(ReportAggregate),
    )


@pytest.mark.parametrize(
    ("policy", "options"),
    [
        ({}, {}),
        ({"gov.irs.credits.ctc.amount.base[0].amount": 3_000}, {}),
        ({}, {"include_cliffs": True}),
    ],
    ids=["current-law", "reform", "cliffs"],
)
def test_a_simulation_planned_alone_gets_its_self_comparisons_plan(
    policy, options
) -> None:
    standalone = _simulation(
        "us", SimulationRole.STANDALONE, policy=policy, options=options
    )

    plan = resolve_simulation_output_plan(standalone)

    assert plan == resolve_report_output_plan(_self_comparison(standalone))
    assert plan.requirements.include_cliff_impacts is bool(options)


# Household columns a UK local-area report run's simulations must write: the
# identifier, the income whose change disclosure control measures, the
# weights, and the area columns copied from the local-area dataset.
LOCAL_AREA_HOUSEHOLD_COLUMNS = {
    "household_id",
    "household_net_income",
    "household_weight",
    "constituency_code_oa",
    "la_code_oa",
    "region_code_oa",
    "source_household_id",
}
UK_INCOME_ELASTICITY = "gov.simulation.labour_supply_responses.income_elasticity"


def _local_area_report(
    *, region: str = "uk", reform: dict[str, JsonValue] | None = None
) -> ReportExecutionInput:
    return _report("uk", region=region, reform=reform, aggregates=LOCAL_AREA_AGGREGATES)


@pytest.mark.parametrize("region", ["uk", "country/wales"])
def test_a_local_area_plan_writes_only_the_household_columns_disclosure_reads(
    region: str,
) -> None:
    from policyengine_simulation_executor import uk_local_authority_metadata

    plan = resolve_local_area_output_plan(_local_area_report(region=region))

    assert plan.country == "uk"
    assert plan.requirements.aggregates == LOCAL_AREA_AGGREGATES
    assert plan.requirements.include_cliff_impacts is False
    assert [entity.entity for entity in plan.entities] == ["household"]
    household = plan.entities[0]
    assert set(household.materialized_variables) == LOCAL_AREA_HOUSEHOLD_COLUMNS
    assert household.additional_variables == ()
    assert household.dataset_variables == (
        uk_local_authority_metadata.UK_AREA_DATASET_COLUMNS
    )


def test_a_local_area_plan_has_the_same_columns_for_every_policy_pair() -> None:
    current_law = resolve_local_area_output_plan(_local_area_report())
    with_responses = resolve_local_area_output_plan(
        _local_area_report(reform={UK_INCOME_ELASTICITY: 0.1})
    )

    assert with_responses.requirements.labor_supply_response_active is True
    assert current_law.requirements.labor_supply_response_active is False
    assert with_responses.entities == current_law.entities


def test_a_local_area_plan_is_refused_for_a_us_report() -> None:
    with pytest.raises(ValueError, match="UK"):
        resolve_local_area_output_plan(_report("us", aggregates=LOCAL_AREA_AGGREGATES))


def test_a_local_area_plan_is_refused_with_cliff_impacts() -> None:
    report = _report(
        "uk", include_cliffs=True, aggregates=LOCAL_AREA_AGGREGATES, region="uk"
    )

    with pytest.raises(ValueError, match="calculates no cliff impacts"):
        resolve_local_area_output_plan(report)


def test_a_uk_simulation_satisfies_the_local_area_plan_without_extra_variables() -> (
    None
):
    from policyengine.tax_benefit_models import uk

    plan = resolve_local_area_output_plan(_local_area_report())
    simulation = Simulation.model_construct(
        policy={},
        dynamic=None,
        dataset=None,
        scoping_strategy=None,
        extra_variables={},
        tax_benefit_model_version=uk.model,
        output_dataset=None,
    )

    apply_output_plan(simulation, plan)

    assert simulation.extra_variables == {"household": []}
