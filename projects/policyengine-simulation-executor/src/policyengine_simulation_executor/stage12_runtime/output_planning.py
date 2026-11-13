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
from policyengine.outputs.extra_variables import add_extra_variables
from policyengine.outputs.labor_supply_response import (
    UK_ACTIVE_LSR_VARIABLES,
    US_ACTIVE_LSR_VARIABLES,
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

from .local_area_datasets import calculates_on_local_area_dataset

if TYPE_CHECKING:
    import pandas as pd
    from policyengine_simulation_contract.stage12_bundle import CountryId

# Input-dataset columns the UK constituency and local-authority breakdowns
# read, the same ones a routed local-area dataset is checked for as it loads.
UK_GEOGRAPHIC_DATASET_VARIABLES: dict[str, tuple[str, ...]] = {
    "household": UK_AREA_DATASET_COLUMNS,
}

# The inputs .py's labour-supply output reads from both simulations of a
# report that turns responses on: copied input values, not responses, so a
# US run stores them whatever its own policy. Every other column .py adds
# for labour supply is a response, zero in a simulation whose own policy
# turns none on (``labor_supply_response_columns``).
LABOR_SUPPLY_COPIED_INPUTS: dict[str, dict[str, tuple[str, ...]]] = {
    "us": {"person": ("self_employment_income", "weekly_hours_worked")},
}
_PY_LABOR_SUPPLY_COLUMNS: dict[str, dict[str, list[str]]] = {
    "us": US_ACTIVE_LSR_VARIABLES,
    "uk": UK_ACTIVE_LSR_VARIABLES,
}


def labor_supply_response_columns(country: str) -> dict[str, tuple[str, ...]]:
    """Return, by entity, the labour-supply response columns of ``country``'s output.

    They are zero in a simulation whose own policy turns no response on, so a
    run of one stores none, and a report reads them as zeros from it.
    """
    copied = LABOR_SUPPLY_COPIED_INPUTS.get(country, {})
    return {
        entity: tuple(
            column for column in columns if column not in copied.get(entity, ())
        )
        for entity, columns in _PY_LABOR_SUPPLY_COLUMNS[country].items()
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


def _include_cliff_impacts(simulation: SimulationExecutionInput) -> bool:
    value = simulation.options.get("include_cliffs", False)
    if not isinstance(value, bool):
        msg = "include_cliffs must be a boolean"
        raise TypeError(msg)
    return value


def _required_dataset_variables(
    simulation: SimulationExecutionInput,
    *,
    aggregates: tuple[ReportAggregate, ...],
) -> dict[str, tuple[str, ...]]:
    """Return input-dataset columns copied into the materialized output.

    A UK report's geographic aggregate reads the area codes of its own
    simulations, except in a national or nation-level report whose bundle
    routes UK area regions to another dataset. Its national dataset may then
    carry no area codes, so none are planned, and the aggregate leaves the
    area breakdowns out (``build_aggregate_report``). A UK-wide or nation
    simulation on the dataset the release routes area regions to
    (``calculates_on_local_area_dataset``) has them, and a local-area report
    reads them, so they are planned there too, also when the simulation is
    planned alone: its run's file then holds what a local-area report reads.
    """
    if (
        simulation.geography.country != "uk"
        or ReportAggregate.GEOGRAPHIC not in aggregates
    ):
        return {}
    if not (
        uk_area_codes_required(simulation.geography.region)
        or calculates_on_local_area_dataset(simulation)
    ):
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
    return _resolve_output_plan(
        report.baseline,
        report.reform,
        aggregates=report.requested_aggregates,
        stage_scope=stage_scope,
    )


def resolve_simulation_output_plan(
    simulation: SimulationExecutionInput,
    *,
    stage_scope: StageScope = _unmonitored_stage_scope,
) -> Stage12OutputPlan:
    """Resolve the output schema every run of ``simulation`` stores.

    Identical requests share simulation runs, so the schema depends on the
    simulation alone, never on a report naming it: the columns of a report
    comparing the simulation with itself, with every aggregate, so the area
    columns on the local-area dataset and the US budget columns too; the
    labour-supply responses only when its own policy turns them on; the US
    inputs .py's labour-supply output reads from either side
    (``LABOR_SUPPLY_COPIED_INPUTS``); and never the cliff columns, so a US
    national run keeps its region-group split. A report reads from it what
    ``side_read_plan`` says.
    """
    return _resolve_output_plan(
        simulation,
        simulation,
        aggregates=tuple(ReportAggregate),
        stage_scope=stage_scope,
        include_cliff_impacts=False,
        copy_labor_supply_inputs=True,
    )


def side_read_plan(
    read_plan: Stage12OutputPlan, stored_plan: Stage12OutputPlan
) -> Stage12OutputPlan:
    """Return what a report planned ``read_plan`` reads from a side storing a plan.

    That is the report's plan, without the labour-supply responses when the
    report turns responses on and the side's own policy turns none on: those
    are zero, and the report reads them as zeros
    (``with_zero_labor_supply_responses``).
    """
    if not _reads_unstored_responses(read_plan, stored_plan):
        return read_plan
    responses = labor_supply_response_columns(read_plan.country)
    entities = []
    for entity in read_plan.entities:
        unstored = set(responses.get(entity.entity, ()))
        materialized = tuple(
            variable
            for variable in entity.materialized_variables
            if variable not in unstored
        )
        if not materialized:
            continue
        entities.append(
            EntityOutputPlan(
                entity=entity.entity,
                materialized_variables=materialized,
                additional_variables=tuple(
                    variable
                    for variable in entity.additional_variables
                    if variable not in unstored
                ),
                dataset_variables=entity.dataset_variables,
            )
        )
    return Stage12OutputPlan(
        country=read_plan.country,
        requirements=read_plan.requirements,
        entities=tuple(entities),
    )


def with_zero_labor_supply_responses(
    frames: Mapping[str, pd.DataFrame],
    read_plan: Stage12OutputPlan,
    stored_plan: Stage12OutputPlan,
) -> Mapping[str, pd.DataFrame]:
    """Return a side's frames with the responses the report reads and it doesn't store.

    Each such response column the report's plan names is added as zeros;
    ``frames`` itself is returned when there is none to add.
    """
    if not _reads_unstored_responses(read_plan, stored_plan):
        return frames
    planned = {
        entity.entity: set(entity.materialized_variables)
        for entity in read_plan.entities
    }
    filled = dict(frames)
    for entity, columns in labor_supply_response_columns(read_plan.country).items():
        if entity not in frames:
            continue
        missing = [
            column
            for column in columns
            if column in planned.get(entity, set())
            and column not in frames[entity].columns
        ]
        if missing:
            filled[entity] = frames[entity].assign(**dict.fromkeys(missing, 0.0))
    return filled


def _reads_unstored_responses(
    read_plan: Stage12OutputPlan, stored_plan: Stage12OutputPlan
) -> bool:
    """Whether the report reads labour-supply responses the side doesn't store."""
    return (
        read_plan.requirements.labor_supply_response_active
        and not stored_plan.requirements.labor_supply_response_active
    )


def _resolve_output_plan(  # noqa: PLR0913  # the two simulations, then keyword-only choices
    baseline_input: SimulationExecutionInput,
    reform_input: SimulationExecutionInput,
    *,
    aggregates: tuple[ReportAggregate, ...],
    stage_scope: StageScope,
    include_cliff_impacts: bool | None = None,
    copy_labor_supply_inputs: bool = False,
) -> Stage12OutputPlan:
    """Resolve one country-owned output schema for a baseline and a reform.

    ``include_cliff_impacts`` None follows the baseline's ``include_cliffs``
    option. ``copy_labor_supply_inputs`` adds ``LABOR_SUPPLY_COPIED_INPUTS``.
    """
    country = baseline_input.geography.country
    stage_attributes = {"country": country}
    with stage_scope(Stage.STAGE12_COUNTRY_MODEL_LOAD, stage_attributes):
        model = _country_model(country)
    with stage_scope(Stage.STAGE12_OUTPUT_CONFIGURATION, stage_attributes):
        baseline = _planning_simulation(baseline_input, model=model)
        reform = _planning_simulation(reform_input, model=model)
        if include_cliff_impacts is None:
            include_cliff_impacts = _include_cliff_impacts(baseline_input)
        labor_supply_active = _configure_country_outputs(
            country=country,
            baseline=baseline,
            reform=reform,
            include_cliff_impacts=include_cliff_impacts,
        )
        if copy_labor_supply_inputs:
            copied = LABOR_SUPPLY_COPIED_INPUTS.get(country, {})
            for simulation in (baseline, reform):
                add_extra_variables(
                    simulation,
                    {entity: list(columns) for entity, columns in copied.items()},
                )
        requirements = ReportOutputRequirements(
            aggregates=aggregates,
            include_cliff_impacts=include_cliff_impacts,
            labor_supply_response_active=labor_supply_active,
        )
    with stage_scope(Stage.STAGE12_OUTPUT_VARIABLE_RESOLUTION, stage_attributes):
        baseline_variables = model.resolve_entity_variables(baseline)
        reform_variables = model.resolve_entity_variables(reform)
        dataset_variables = _required_dataset_variables(
            baseline_input, aggregates=requirements.aggregates
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
