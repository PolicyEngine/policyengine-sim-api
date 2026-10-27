"""The export file of v1 report definitions, schema version 1.

The Public API's one-time migration command exports one definition per v1
report that an account row refers to. Each definition already holds v2
references: the v2 policy and household IDs that ``legacy_policy_mappings``
and ``legacy_household_mappings`` map the v1 IDs to, and the region code that
the Public API's region reference files map the v1 region to. This module
reads no Public API table and no reference file. ``README.md`` next to it
describes the format with an example.

Every field is required, even where null is allowed, so a field the export
leaves out is an error rather than a silent default. Numbers are never
converted from text.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PositiveInt,
    model_validator,
)

from policyengine_simulation_persistence.reports import (
    FIRST_YEAR,
    LAST_YEAR,
    NewReport,
    ReportKind,
)
from policyengine_simulation_persistence.simulations import (
    NewSimulation,
    SimulationKind,
)

if TYPE_CHECKING:
    from typing import Self

REPORT_DEFINITIONS_SCHEMA_VERSION = 1


class _DefinitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EconomySimulationDefinition(_DefinitionModel):
    """One v1 economy simulation: a policy applied to a region."""

    legacy_simulation_id: PositiveInt
    # The v2 policy ID, or null for current law.
    policy_id: UUID | None
    # The v2 region code, such as ``state/ca`` or ``constituency/E14001063``.
    region_code: str
    # A dataset name in the catalog, or null for the region's default dataset.
    dataset_name: str | None

    def new_simulation(self, country: str) -> NewSimulation:
        """Return the simulation to create for this v1 simulation."""
        return NewSimulation(
            kind=SimulationKind.ECONOMY,
            country=country,
            policy_id=self.policy_id,
            region_code=self.region_code,
            dataset_name=self.dataset_name,
            legacy_simulation_id=self.legacy_simulation_id,
        )


class HouseholdSimulationDefinition(_DefinitionModel):
    """One v1 household simulation: a policy applied to a household."""

    legacy_simulation_id: PositiveInt
    # The v2 policy ID, or null for current law.
    policy_id: UUID | None
    # The v2 household ID.
    household_id: UUID

    def new_simulation(self, country: str) -> NewSimulation:
        """Return the simulation to create for this v1 simulation."""
        return NewSimulation(
            kind=SimulationKind.HOUSEHOLD,
            country=country,
            policy_id=self.policy_id,
            household_id=self.household_id,
            legacy_simulation_id=self.legacy_simulation_id,
        )


type SimulationDefinition = EconomySimulationDefinition | HouseholdSimulationDefinition


class _ReportDefinition(_DefinitionModel):
    legacy_report_id: PositiveInt
    country: str
    year: Annotated[int, Field(ge=FIRST_YEAR, le=LAST_YEAR)]
    options: dict[str, JsonValue]

    def simulations(self) -> tuple[SimulationDefinition, ...]:
        """Return the baseline, then the reform if the report has one."""
        raise NotImplementedError

    def report_kind(self) -> ReportKind:
        """Return the kind of the report and of its simulations."""
        raise NotImplementedError

    def new_simulation(self, simulation: SimulationDefinition) -> NewSimulation:
        """Return the simulation to create for one of this report's simulations."""
        return simulation.new_simulation(self.country)

    def new_report(
        self, baseline_simulation_id: UUID, reform_simulation_id: UUID | None
    ) -> NewReport:
        """Return the report to create once its simulations exist."""
        return NewReport(
            kind=self.report_kind(),
            country=self.country,
            baseline_simulation_id=baseline_simulation_id,
            reform_simulation_id=reform_simulation_id,
            year=self.year,
            options=self.options,
            legacy_report_id=self.legacy_report_id,
        )

    def _check_simulations(self) -> None:
        # The simulation tables' own checks, run before anything is written.
        for simulation in self.simulations():
            self.new_simulation(simulation)


class EconomyReportDefinition(_ReportDefinition):
    """A v1 economy report: a reform and a baseline policy on one region."""

    kind: Literal["economy"]
    baseline: EconomySimulationDefinition
    reform: EconomySimulationDefinition

    def simulations(self) -> tuple[SimulationDefinition, ...]:
        """Return the baseline, then the reform."""
        return (self.baseline, self.reform)

    def report_kind(self) -> ReportKind:
        """Return ``economy``."""
        return ReportKind.ECONOMY

    @model_validator(mode="after")
    def _check_population(self) -> Self:
        self._check_simulations()
        population = (self.baseline.region_code, self.baseline.dataset_name)
        if (self.reform.region_code, self.reform.dataset_name) != population:
            msg = (
                "an economy report's simulations must have "
                "the same region_code and dataset_name"
            )
            raise ValueError(msg)
        return self


class HouseholdReportDefinition(_ReportDefinition):
    """A v1 household report: one household under one policy, or under two."""

    kind: Literal["household"]
    baseline: HouseholdSimulationDefinition
    reform: HouseholdSimulationDefinition | None

    def simulations(self) -> tuple[SimulationDefinition, ...]:
        """Return the baseline, then the reform if there is one."""
        if self.reform is None:
            return (self.baseline,)
        return (self.baseline, self.reform)

    def report_kind(self) -> ReportKind:
        """Return ``household``."""
        return ReportKind.HOUSEHOLD

    @model_validator(mode="after")
    def _check_population(self) -> Self:
        self._check_simulations()
        if self.reform is not None and (
            self.reform.household_id != self.baseline.household_id
        ):
            msg = "a household report's simulations must have the same household_id"
            raise ValueError(msg)
        return self


type ReportDefinition = Annotated[
    EconomyReportDefinition | HouseholdReportDefinition,
    Field(discriminator="kind"),
]


def _listed(legacy_ids: set[int]) -> str:
    return ", ".join(str(legacy_id) for legacy_id in sorted(legacy_ids))


class ReportDefinitionsFile(_DefinitionModel):
    """The whole export: its schema version and every report definition."""

    schema_version: Literal[1]
    reports: list[ReportDefinition]

    @model_validator(mode="after")
    def _check_legacy_ids(self) -> Self:
        report_counts = Counter(report.legacy_report_id for report in self.reports)
        repeated_reports = {
            legacy_id for legacy_id, count in report_counts.items() if count > 1
        }
        if repeated_reports:
            msg = f"v1 reports appear more than once: {_listed(repeated_reports)}"
            raise ValueError(msg)
        descriptions: dict[int, NewSimulation] = {}
        conflicting: set[int] = set()
        for report in self.reports:
            for simulation in report.simulations():
                new_simulation = report.new_simulation(simulation)
                legacy_id = simulation.legacy_simulation_id
                if descriptions.setdefault(legacy_id, new_simulation) != new_simulation:
                    conflicting.add(legacy_id)
        if conflicting:
            msg = (
                "v1 simulations are described differently in different reports: "
                f"{_listed(conflicting)}"
            )
            raise ValueError(msg)
        return self
