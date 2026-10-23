"""Resolve, apply, and verify Stage 12 simulation output schemas."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Protocol, cast

from policyengine.core import Simulation
from policyengine.outputs import (
    configure_cliff_impact_variables,
    configure_labor_supply_response_variables,
    labor_supply_response_is_active,
)
from policyengine_simulation_contract.stage12_execution import (
    EntityOutputPlan,
    PlannedSimulationExecutionInput,
    ReportAggregate,
    ReportExecutionInput,
    ReportOutputRequirements,
    SimulationExecutionInput,
    Stage12OutputPlan,
)
from policyengine_simulation_observability.stages import Stage

from policyengine_simulation_executor.uk_local_authority_metadata import (
    UK_AREA_DATASET_COLUMNS,
    uk_area_codes_required,
)

if TYPE_CHECKING:
    import pandas as pd
    from policyengine_simulation_contract.stage12_bundle import CountryId

# Input-dataset columns the UK constituency and local-authority breakdowns
# read, the same ones a routed local-area dataset is checked for as it loads.
UK_GEOGRAPHIC_DATASET_VARIABLES: dict[str, tuple[str, ...]] = {
    "household": UK_AREA_DATASET_COLUMNS,
}


class OutputVariableModel(Protocol):
    """Country-model behavior required by the Stage 12 planner."""

    def resolve_entity_variables(
        self,
        simulation: Simulation,
    ) -> dict[str, list[str]]:
        """Return the output variables each entity must carry for ``simulation``."""
        ...


StageScope = Callable[
    [Stage, Mapping[str, object] | None],
    AbstractContextManager[object],
]


def _unmonitored_stage_scope(
    _stage: Stage,
    _attributes: Mapping[str, object] | None = None,
) -> AbstractContextManager[object]:
    return nullcontext()


def _country_model(country: CountryId) -> OutputVariableModel:
    if country == "us":
        from policyengine.tax_benefit_models import (
            us,
        )

        return us.model
    from policyengine.tax_benefit_models import (
        uk,
    )

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
        msg = "include_cliffs must be a boolean"
        raise TypeError(msg)
    return value


def _required_dataset_variables(
    *,
    country: CountryId,
    region: str,
    aggregates: tuple[ReportAggregate, ...],
) -> dict[str, tuple[str, ...]]:
    """Return input-dataset columns copied into the materialized output.

    A UK report's geographic aggregate reads the area codes of its own
    simulations, except in a national or nation-level report whose bundle
    routes UK area regions to another dataset. Its national dataset may then
    carry no area codes, so none are planned, and the aggregate leaves the
    area breakdowns out (``build_aggregate_report``).
    """
    if country != "uk" or ReportAggregate.GEOGRAPHIC not in aggregates:
        return {}
    if not uk_area_codes_required(region):
        return {}
    return UK_GEOGRAPHIC_DATASET_VARIABLES


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


def resolve_report_output_plan(
    report: ReportExecutionInput,
    *,
    stage_scope: StageScope = _unmonitored_stage_scope,
) -> Stage12OutputPlan:
    """Resolve one country-owned output schema for both report simulations."""
    country = report.baseline.geography.country
    stage_attributes = {"country": country}
    with stage_scope(Stage.STAGE12_COUNTRY_MODEL_LOAD, stage_attributes):
        model = _country_model(country)
    with stage_scope(Stage.STAGE12_OUTPUT_CONFIGURATION, stage_attributes):
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
    with stage_scope(Stage.STAGE12_OUTPUT_VARIABLE_RESOLUTION, stage_attributes):
        baseline_variables = model.resolve_entity_variables(baseline)
        reform_variables = model.resolve_entity_variables(reform)
        dataset_variables = _required_dataset_variables(
            country=country,
            region=report.baseline.geography.region,
            aggregates=requirements.aggregates,
        )
        entities = []
        for entity in sorted(
            set(baseline_variables) | set(reform_variables) | set(dataset_variables)
        ):
            entity_dataset_variables = dataset_variables.get(entity, ())
            materialized = tuple(
                sorted(
                    set(baseline_variables.get(entity, ()))
                    | set(reform_variables.get(entity, ()))
                    | set(entity_dataset_variables)
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
                    dataset_variables=entity_dataset_variables,
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
    model = cast("OutputVariableModel", simulation.tax_benefit_model_version)
    resolved = model.resolve_entity_variables(simulation)
    missing = {
        entity_plan.entity: sorted(
            (
                set(entity_plan.materialized_variables)
                - set(entity_plan.dataset_variables)
            )
            - set(resolved.get(entity_plan.entity, ()))
        )
        for entity_plan in output_plan.entities
    }
    missing = {entity: variables for entity, variables in missing.items() if variables}
    if missing:
        msg = f"configured simulation does not satisfy output plan: {missing}"
        raise ValueError(msg)


def validate_output_frames(
    frames: Mapping[str, pd.DataFrame],
    output_plan: Stage12OutputPlan,
) -> None:
    """Require every planned entity and column in materialized output frames."""
    missing_entities = [
        entity.entity for entity in output_plan.entities if entity.entity not in frames
    ]
    if missing_entities:
        msg = f"simulation output is missing planned entities: {missing_entities}"
        raise ValueError(msg)
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
        msg = f"simulation output is missing planned columns: {missing_columns}"
        raise ValueError(msg)
