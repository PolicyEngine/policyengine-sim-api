"""Request and response models of the household calculation routes.

The Public API sends what the Simulation Entrypoint cannot read itself: the
household in the situation shape the household functions take, with its
saved SPM choice, and each policy's parameter values. The Simulation
Entrypoint records the calculation against the Public API's IDs (household,
policy, catalog version), whose rows the database checks exist.

This is v2 code: the main path's ``run_simulation`` does not use it.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Self
from uuid import UUID

from policyengine_simulation_contract.household_execution import (
    MAXIMUM_POLICY_PARAMETERS,
    Axes,
    HouseholdDocument,
    ParameterPath,
    ParameterPeriod,
    ParameterValue,
)
from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_persistence import (
    NewSimulation,
    RunStatus,
    RunTrigger,
    SimulationKind,
)
from policyengine_simulation_persistence.reports import FIRST_YEAR, LAST_YEAR
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from policyengine_simulation_persistence import (
        HouseholdCalculation,
        HouseholdReport,
    )

PolicyParameters = Annotated[
    dict[ParameterPath, dict[ParameterPeriod, ParameterValue]],
    Field(max_length=MAXIMUM_POLICY_PARAMETERS),
]


class StrictRouteModel(BaseModel):
    """Base for the household routes' models: no unknown fields, finite numbers."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class HouseholdPolicyInput(StrictRouteModel):
    """A policy: its Public API ID, or none for current law, and its values.

    ``parameters`` maps each changed parameter path to its values by period
    range (``"YYYY-MM-DD.YYYY-MM-DD"``), as in the Public API's policy JSON.
    Current law changes no parameters.
    """

    policy_id: UUID | None = None
    parameters: PolicyParameters = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_a_policy_for_parameters(self) -> Self:
        """Refuse parameter values without the policy they belong to."""
        if self.policy_id is None and self.parameters:
            msg = "current law (no policy_id) changes no parameters"
            raise ValueError(msg)
        return self


class HouseholdCalculationOptions(StrictRouteModel):
    """What else changes what a household calculation computes.

    ``axes`` vary inputs across points, and ``add_missing_variables`` adds
    every yearly, monthly and eternal variable the household lacks, as the
    Public API's ``/calculate-full`` does. The options are part of what a
    calculation is, so a calculation is reused only for the same options.
    """

    axes: Axes = None
    add_missing_variables: bool = False

    def stored(self) -> dict[str, Any]:
        """Return the options a simulation stores: only those set, as JSON."""
        stored: dict[str, Any] = {}
        if self.axes is not None:
            stored["axes"] = [
                [axis.model_dump(mode="json") for axis in group] for group in self.axes
            ]
        if self.add_missing_variables:
            stored["add_missing_variables"] = True
        return stored


PolicyEngineVersion = Annotated[str, Field(min_length=1, max_length=255)]
Year = Annotated[int, Field(ge=FIRST_YEAR, le=LAST_YEAR)]


class StoredHouseholdInputs(StrictRouteModel):
    """A stored household and the catalog version to calculate it on.

    ``catalog_version_id`` is the Public API's catalog version and
    ``policyengine_version`` the PolicyEngine.py version it was published
    from, which picks the household function. ``household`` is the stored
    household in the situation shape, without axes (they are an option);
    ``spm`` is its saved SPM choice, omitted when it has none, so the
    bundle's certified defaults apply.
    """

    catalog_version_id: UUID
    policyengine_version: PolicyEngineVersion
    household_id: UUID
    household: HouseholdDocument
    spm: JsonValue = None

    @field_validator("household")
    @classmethod
    def refuse_axes_in_the_household(
        cls, household: HouseholdDocument
    ) -> HouseholdDocument:
        """Require axes in ``options``, where they are part of the calculation."""
        if household.has_axes:
            msg = "send axes in options, not in the household"
            raise ValueError(msg)
        return household

    @property
    def spm_requested(self) -> bool:
        """Whether the household has a saved SPM choice to calculate with."""
        return "spm" in self.model_fields_set


class HouseholdCalculationSubmission(StoredHouseholdInputs):
    """One household under one policy, to calculate for a catalog version and year.

    A calculation submitted again reuses its run while it is unfinished or
    once it succeeded, unless ``force``.
    """

    country: CountryId
    year: Year
    policy: HouseholdPolicyInput = Field(default_factory=HouseholdPolicyInput)
    options: HouseholdCalculationOptions = Field(
        default_factory=HouseholdCalculationOptions
    )
    force: bool = False
    trigger: RunTrigger = RunTrigger.INITIAL

    def simulation_definition(self) -> NewSimulation:
        """Return the household simulation this calculation runs."""
        return household_simulation_definition(
            self, self.country, self.policy, self.options
        )


class HouseholdReportSubmission(StoredHouseholdInputs):
    """A household report to create: the household under a baseline and a reform.

    ``reform`` is omitted for a report of the baseline alone. Both policies
    are calculated with the same ``options``, in one Modal call when neither
    has a run to reuse. The report's first run starts at once.
    """

    country: CountryId
    year: Year
    baseline: HouseholdPolicyInput = Field(default_factory=HouseholdPolicyInput)
    reform: HouseholdPolicyInput | None = None
    options: HouseholdCalculationOptions = Field(
        default_factory=HouseholdCalculationOptions
    )
    force: bool = False
    trigger: RunTrigger = RunTrigger.INITIAL

    def simulation_definition(self, policy: HouseholdPolicyInput) -> NewSimulation:
        """Return the household simulation of one of the report's policies."""
        return household_simulation_definition(self, self.country, policy, self.options)


class HouseholdReportRunSubmission(StoredHouseholdInputs):
    """Another run of a stored household report, on a catalog version.

    The household and both policies must be the report's own: the Public API
    sends their contents again, because the Simulation Entrypoint cannot
    read them. The runs are reused while unfinished or once they succeeded,
    unless ``force``.
    """

    baseline: HouseholdPolicyInput = Field(default_factory=HouseholdPolicyInput)
    reform: HouseholdPolicyInput | None = None
    force: bool = False
    trigger: RunTrigger = RunTrigger.MANUAL


def household_simulation_definition(
    inputs: StoredHouseholdInputs,
    country: str,
    policy: HouseholdPolicyInput,
    options: HouseholdCalculationOptions,
) -> NewSimulation:
    """Return the household simulation of ``inputs`` under ``policy``."""
    return NewSimulation(
        kind=SimulationKind.HOUSEHOLD,
        country=country,
        household_id=inputs.household_id,
        policy_id=policy.policy_id,
        options=options.stored(),
    )


class HouseholdCalculationResult(StrictRouteModel):
    """A succeeded calculation's household, warnings, SPM choice and bundle.

    ``household`` keeps the request's people, groups and axes with each
    requested value filled in, in the caller's key order. ``spm_config`` is
    the complete SPM selection applied and ``spm_provenance`` its receipt,
    both null for a country without SPM; ``bundle`` names the
    PolicyEngine.py bundle the calculation ran with.
    """

    household: dict[str, JsonValue]
    warnings: list[str]
    spm_config: dict[str, JsonValue] | None
    spm_provenance: dict[str, JsonValue] | None
    bundle: dict[str, JsonValue] | None


class HouseholdCalculationFailure(StrictRouteModel):
    """Why a calculation failed.

    ``code`` is the run's error code, such as ``household_calculation_failed``
    (the household function refused the inputs; ``detail`` holds its error,
    with the Public API's error codes and messages), ``worker_error``,
    ``worker_timeout`` or ``dispatch_abandoned`` (submit it again).
    """

    code: str
    message: str
    detail: dict[str, JsonValue] | None


class HouseholdCalculationResponse(StrictRouteModel):
    """A household calculation's run: what it calculates, its status and outcome.

    ``calculation_id`` is the simulation run's ID. ``result`` is set once the
    status is ``succeeded`` and ``error`` once it is ``failed``.
    """

    calculation_id: UUID
    simulation_id: UUID
    country: str
    household_id: UUID
    policy_id: UUID | None
    options: dict[str, JsonValue]
    catalog_version_id: UUID
    year: int
    attempt: int
    trigger: RunTrigger
    status: RunStatus
    created_at: datetime
    dispatched_at: datetime | None
    completed_at: datetime | None
    result: HouseholdCalculationResult | None
    error: HouseholdCalculationFailure | None

    @classmethod
    def from_calculation(cls, calculation: HouseholdCalculation) -> Self:
        """Describe a stored household calculation."""
        run, simulation, output = (
            calculation.run,
            calculation.simulation,
            calculation.output,
        )
        # The database requires a household simulation to name its household.
        if simulation.household_id is None:  # pragma: no cover
            msg = "a household calculation's simulation has a household"
            raise ValueError(msg)
        return cls(
            calculation_id=run.id,
            simulation_id=simulation.id,
            country=simulation.country,
            household_id=simulation.household_id,
            policy_id=simulation.policy_id,
            options=simulation.options,
            catalog_version_id=run.tax_benefit_model_version_id,
            year=run.year,
            attempt=run.attempt,
            trigger=run.trigger,
            status=run.status,
            created_at=run.created_at,
            dispatched_at=run.dispatched_at,
            completed_at=run.completed_at,
            result=(
                None
                if output is None
                else HouseholdCalculationResult(
                    household=output.household,
                    warnings=list(output.warnings),
                    spm_config=output.spm_config,
                    spm_provenance=output.spm_provenance,
                    bundle=output.bundle,
                )
            ),
            error=(
                None
                if run.status is not RunStatus.FAILED
                else HouseholdCalculationFailure(
                    code=run.error_code or "unknown",
                    message=run.error_message or "",
                    detail=run.error_detail,
                )
            ),
        )


def household_report_status(report: HouseholdReport) -> RunStatus | None:
    """Return a household report run's status, from its simulation runs.

    It has failed when either run failed and succeeded when both did;
    otherwise it is the less advanced of the two. None before the first run.
    """
    statuses = {
        calculation.run.status
        for calculation in (report.baseline, report.reform)
        if calculation is not None
    }
    if not statuses:
        return None
    if RunStatus.FAILED in statuses:
        return RunStatus.FAILED
    for status in (RunStatus.PENDING, RunStatus.DISPATCHING, RunStatus.RUNNING):
        if status in statuses:
            return status
    return RunStatus.SUCCEEDED


class HouseholdReportResponse(StrictRouteModel):
    """A household report with one of its runs, and that run's two calculations.

    The run is the report's newest, unless the response answers the request
    that started another. ``status`` is the run's status: failed when either
    calculation failed, succeeded when both did, and otherwise the less
    advanced of the two. The run fields are null before the first run;
    ``reform`` is null for a report of the baseline alone.
    """

    report_id: UUID
    country: str
    year: int
    household_id: UUID | None
    baseline_simulation_id: UUID
    reform_simulation_id: UUID | None
    report_run_id: UUID | None
    catalog_version_id: UUID | None
    trigger: RunTrigger | None
    status: RunStatus | None
    baseline: HouseholdCalculationResponse | None
    reform: HouseholdCalculationResponse | None

    @classmethod
    def from_report(cls, report: HouseholdReport) -> Self:
        """Describe a stored household report and one of its runs."""
        report_run = report.report_run
        baseline = (
            None
            if report.baseline is None
            else HouseholdCalculationResponse.from_calculation(report.baseline)
        )
        return cls(
            report_id=report.report.id,
            country=report.report.country,
            year=report.report.year,
            household_id=None if baseline is None else baseline.household_id,
            baseline_simulation_id=report.report.baseline_simulation_id,
            reform_simulation_id=report.report.reform_simulation_id,
            report_run_id=None if report_run is None else report_run.id,
            catalog_version_id=(
                None if report_run is None else report_run.tax_benefit_model_version_id
            ),
            trigger=None if report_run is None else report_run.trigger,
            status=household_report_status(report),
            baseline=baseline,
            reform=(
                None
                if report.reform is None
                else HouseholdCalculationResponse.from_calculation(report.reform)
            ),
        )


class HouseholdRouteError(BaseModel):
    """Why a household route refused or could not serve a request."""

    detail: str
