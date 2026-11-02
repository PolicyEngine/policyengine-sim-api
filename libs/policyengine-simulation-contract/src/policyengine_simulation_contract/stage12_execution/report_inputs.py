"""A report's simulation pair, its required outputs, and the planned output schema."""

from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

# Models subclass these bases through the package's path, which ruff.toml
# lists as runtime-evaluated; base.py binds them in the package first.
from policyengine_simulation_contract.stage12_execution import (
    SimulationExecutionInput,
    StrictContractModel,
)
from policyengine_simulation_contract.stage12_execution.base import (
    ContractText,
    CountryId,
    SimulationRole,
)


class ReportAggregate(StrEnum):
    """A family of aggregate results that a comparison report computes."""

    BUDGET = "budget"
    POVERTY = "poverty"
    INEQUALITY = "inequality"
    DISTRIBUTIONAL = "distributional"
    WINNERS_AND_LOSERS = "winners_and_losers"
    GEOGRAPHIC = "geographic"
    PROGRAM_STATISTICS = "program_statistics"


class ReportOutputRequirements(StrictContractModel):
    """Report features that determine which simulation columns must exist."""

    schema_version: Literal[1] = 1
    aggregates: Annotated[tuple[ReportAggregate, ...], Field(min_length=1)]
    include_cliff_impacts: bool
    labor_supply_response_active: bool

    @field_validator("aggregates")
    @classmethod
    def require_complete_aggregate_profile(
        cls,
        value: tuple[ReportAggregate, ...],
    ) -> tuple[ReportAggregate, ...]:
        """Require every ``ReportAggregate``, once each and in declaration order."""
        if len(value) != len(set(value)):
            msg = "report output aggregates must be unique"
            raise ValueError(msg)
        if value != tuple(ReportAggregate):
            msg = "Stage 12 currently requires the complete aggregate profile"
            raise ValueError(msg)
        return value


class EntityOutputPlan(StrictContractModel):
    """Required materialized columns for one country-model entity."""

    entity: ContractText
    materialized_variables: Annotated[
        tuple[ContractText, ...],
        Field(min_length=1),
    ]
    additional_variables: tuple[ContractText, ...] = ()
    dataset_variables: tuple[ContractText, ...] = ()

    @field_validator(
        "materialized_variables",
        "additional_variables",
        "dataset_variables",
    )
    @classmethod
    def require_canonical_variables(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require variable names to be unique and sorted, so plans hash stably."""
        if len(value) != len(set(value)):
            msg = "output-plan variables must be unique"
            raise ValueError(msg)
        if value != tuple(sorted(value)):
            msg = "output-plan variables must use canonical order"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def require_variable_subsets(self) -> EntityOutputPlan:
        """Require the additional and dataset variables not to overlap.

        Both must also be subsets of ``materialized_variables``.
        """
        if not set(self.additional_variables).issubset(self.materialized_variables):
            msg = "additional output-plan variables must be a materialized subset"
            raise ValueError(msg)
        if not set(self.dataset_variables).issubset(self.materialized_variables):
            msg = "dataset output-plan variables must be a materialized subset"
            raise ValueError(msg)
        if set(self.additional_variables).intersection(self.dataset_variables):
            msg = (
                "calculated additional variables and dataset variables must be disjoint"
            )
            raise ValueError(msg)
        return self


class Stage12OutputPlan(StrictContractModel):
    """One immutable output schema shared by both report simulations."""

    schema_version: Literal[1] = 1
    country: CountryId
    requirements: ReportOutputRequirements
    entities: Annotated[tuple[EntityOutputPlan, ...], Field(min_length=1)]

    @field_validator("entities")
    @classmethod
    def require_canonical_entities(
        cls,
        value: tuple[EntityOutputPlan, ...],
    ) -> tuple[EntityOutputPlan, ...]:
        """Require entity plans to be unique and sorted by entity name."""
        names = tuple(entity.entity for entity in value)
        if len(names) != len(set(names)):
            msg = "output-plan entities must be unique"
            raise ValueError(msg)
        if names != tuple(sorted(names)):
            msg = "output-plan entities must use canonical order"
            raise ValueError(msg)
        return value


def stage12_output_plan_sha256(plan: Stage12OutputPlan) -> str:
    """Return the stable digest recorded by each Stage 12 child artifact."""
    payload = json.dumps(
        plan.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


class PlannedSimulationExecutionInput(SimulationExecutionInput):
    """Internal coordinator-to-worker input with a resolved output schema."""

    output_plan: Stage12OutputPlan

    @model_validator(mode="after")
    def require_output_plan_country(self) -> PlannedSimulationExecutionInput:
        """Reject an output plan built for a different country than the geography."""
        if self.output_plan.country != self.geography.country:
            msg = "output plan country must match simulation geography"
            raise ValueError(msg)
        return self


class ReportExecutionInput(StrictContractModel):
    """A baseline and reform simulation pair to run and aggregate into one report.

    Both simulations must belong to this report, carry the baseline and reform
    roles and distinct execution IDs, and differ only in their policy.
    """

    contract_version: Literal[1] = 1
    evaluation_id: UUID
    baseline: SimulationExecutionInput
    reform: SimulationExecutionInput
    requested_aggregates: Annotated[tuple[ReportAggregate, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_aligned_simulations(self) -> ReportExecutionInput:
        """Check the two simulations form one comparable report.

        Roles must be baseline and reform, both must belong to this report with
        distinct execution ids, and everything but the policy must match.
        """
        if self.baseline.role is not SimulationRole.BASELINE:
            msg = "baseline input must use the baseline role"
            raise ValueError(msg)
        if self.reform.role is not SimulationRole.REFORM:
            msg = "reform input must use the reform role"
            raise ValueError(msg)
        if self.baseline.evaluation_id != self.evaluation_id:
            msg = "baseline evaluation_id must match the report"
            raise ValueError(msg)
        if self.reform.evaluation_id != self.evaluation_id:
            msg = "reform evaluation_id must match the report"
            raise ValueError(msg)
        if self.baseline.simulation_execution_id == self.reform.simulation_execution_id:
            msg = "baseline and reform simulation identifiers must differ"
            raise ValueError(msg)
        for field_name in (
            "population",
            "year",
            "geography",
            "options",
            "bundle",
        ):
            if getattr(self.baseline, field_name) != getattr(self.reform, field_name):
                msg = f"baseline and reform {field_name} must match before execution"
                raise ValueError(msg)
        if len(self.requested_aggregates) != len(set(self.requested_aggregates)):
            msg = "requested aggregates must be unique"
            raise ValueError(msg)
        return self
