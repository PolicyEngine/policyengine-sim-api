"""Resolve, apply, and verify Stage 12 simulation output schemas."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, cast

import pandas as pd
from policyengine.core import Simulation
from policyengine.outputs import (
    configure_cliff_impact_variables,
    configure_labor_supply_response_variables,
    labor_supply_response_is_active,
)
from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_contract.stage12_execution import (
    EntityOutputPlan,
    PlannedSimulationExecutionInput,
    ReportExecutionInput,
    ReportOutputRequirements,
    SimulationExecutionInput,
    Stage12OutputPlan,
)


class OutputVariableModel(Protocol):
    """Country-model behavior required by the Stage 12 planner."""

    def resolve_entity_variables(
        self,
        simulation: Simulation,
    ) -> dict[str, list[str]]: ...


def _country_model(country: CountryId) -> OutputVariableModel:
    if country == "us":
        from policyengine.tax_benefit_models import us

        return us.model
    from policyengine.tax_benefit_models import uk

    return uk.model


def _planning_simulation(
    simulation: SimulationExecutionInput,
    *,
    model: OutputVariableModel,
) -> Simulation:
    """Build a data-free object used only by PolicyEngine output configurators.

    ``Simulation`` normally requires a dataset. Output planning only needs its
    policy, model version, and ``extra_variables`` fields, so validation is
    intentionally bypassed. This object must never call ``ensure`` or access a
    dataset.
    """

    return Simulation.model_construct(
        id=f"stage12-output-plan-{simulation.role.value}",
        policy=dict(simulation.policy),
        dynamic=None,
        dataset=None,
        scoping_strategy=None,
        extra_variables={},
        tax_benefit_model_version=model,
        output_dataset=None,
    )


def _include_cliff_impacts(report: ReportExecutionInput) -> bool:
    value = report.baseline.options.get("include_cliffs", False)
    if not isinstance(value, bool):
        raise TypeError("include_cliffs must be a boolean")
    return value


def _configure_country_outputs(
    *,
    country: CountryId,
    baseline: Simulation,
    reform: Simulation,
    include_cliff_impacts: bool,
) -> bool:
    labor_supply_active = labor_supply_response_is_active(
        baseline,
        reform,
        country_code=country,
    )
    configure_labor_supply_response_variables(
        baseline,
        reform,
        country_code=country,
    )
    if include_cliff_impacts:
        configure_cliff_impact_variables(baseline, reform)
    if country == "us":
        from policyengine.tax_benefit_models.us.analysis import (
            configure_budgetary_impact_variables,
        )

        configure_budgetary_impact_variables(baseline, reform)
    return labor_supply_active


def resolve_report_output_plan(report: ReportExecutionInput) -> Stage12OutputPlan:
    """Resolve one country-owned output schema for both report simulations."""

    country = report.baseline.geography.country
    model = _country_model(country)
    baseline = _planning_simulation(report.baseline, model=model)
    reform = _planning_simulation(report.reform, model=model)
    include_cliff_impacts = _include_cliff_impacts(report)
    labor_supply_active = _configure_country_outputs(
        country=country,
        baseline=baseline,
        reform=reform,
        include_cliff_impacts=include_cliff_impacts,
    )
    requirements = ReportOutputRequirements(
        aggregates=report.requested_aggregates,
        include_cliff_impacts=include_cliff_impacts,
        labor_supply_response_active=labor_supply_active,
    )
    baseline_variables = model.resolve_entity_variables(baseline)
    reform_variables = model.resolve_entity_variables(reform)
    entities = []
    for entity in sorted(set(baseline_variables) | set(reform_variables)):
        materialized = tuple(
            sorted(
                set(baseline_variables.get(entity, ()))
                | set(reform_variables.get(entity, ()))
            )
        )
        additional = tuple(
            sorted(
                set((baseline.extra_variables or {}).get(entity, ()))
                | set((reform.extra_variables or {}).get(entity, ()))
            )
        )
        entities.append(
            EntityOutputPlan(
                entity=entity,
                materialized_variables=materialized,
                additional_variables=additional,
            )
        )
    return Stage12OutputPlan(
        country=country,
        requirements=requirements,
        entities=tuple(entities),
    )


def plan_simulation_input(
    simulation: SimulationExecutionInput,
    output_plan: Stage12OutputPlan,
) -> PlannedSimulationExecutionInput:
    """Attach a coordinator-resolved schema to one child input."""

    return PlannedSimulationExecutionInput.model_validate(
        {
            **simulation.model_dump(mode="json"),
            "output_plan": output_plan.model_dump(mode="json"),
        }
    )


def apply_output_plan(
    simulation: Simulation,
    output_plan: Stage12OutputPlan,
) -> None:
    """Install required extra variables before a live simulation is ensured."""

    extras = {
        entity: list(variables)
        for entity, variables in (simulation.extra_variables or {}).items()
    }
    for entity_plan in output_plan.entities:
        entity_extras = extras.setdefault(entity_plan.entity, [])
        for variable in entity_plan.additional_variables:
            if variable not in entity_extras:
                entity_extras.append(variable)
    simulation.extra_variables = extras
    model = cast(OutputVariableModel, simulation.tax_benefit_model_version)
    resolved = model.resolve_entity_variables(simulation)
    missing = {
        entity_plan.entity: sorted(
            set(entity_plan.materialized_variables)
            - set(resolved.get(entity_plan.entity, ()))
        )
        for entity_plan in output_plan.entities
    }
    missing = {entity: variables for entity, variables in missing.items() if variables}
    if missing:
        raise ValueError(
            f"configured simulation does not satisfy output plan: {missing}"
        )


def validate_output_frames(
    frames: Mapping[str, pd.DataFrame],
    output_plan: Stage12OutputPlan,
) -> None:
    """Require every planned entity and column in materialized output frames."""

    missing_entities = [
        entity.entity for entity in output_plan.entities if entity.entity not in frames
    ]
    if missing_entities:
        raise ValueError(
            f"simulation output is missing planned entities: {missing_entities}"
        )
    missing_columns = {
        entity.entity: sorted(
            set(entity.materialized_variables) - set(frames[entity.entity].columns)
        )
        for entity in output_plan.entities
    }
    missing_columns = {
        entity: columns for entity, columns in missing_columns.items() if columns
    }
    if missing_columns:
        raise ValueError(
            f"simulation output is missing planned columns: {missing_columns}"
        )
