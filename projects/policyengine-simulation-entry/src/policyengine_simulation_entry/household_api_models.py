"""Request and response models of the household calculation routes.

The Public API sends the IDs of its stored household and policies and the
PolicyEngine.py version to calculate with; the Simulation Entrypoint reads
the household, the policies' parameter values and the catalog version from
the Public API's tables itself (``HouseholdInputReader``), and calculates
the household's ``default_year``.

This is v2 code: the v1 ``run_simulation`` code does not use it.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Self
from uuid import UUID

from policyengine_simulation_contract.household_execution import Axes
from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_persistence import (
    NewSimulation,
    RunStatus,
    RunTrigger,
    SimulationKind,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue

if TYPE_CHECKING:
    from policyengine_simulation_persistence import (
        HouseholdCalculation,
        HouseholdReport,
    )


class StrictRouteModel(BaseModel):
    """Base for the household routes' models: no unknown fields, finite numbers."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class HouseholdPolicyChoice(StrictRouteModel):
    """A policy stored in the Public API, by ID, or none for current law."""

    policy_id: UUID | None = None


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


PolicyEngineVersion = Annotated[str, Field(min_length=1, max_length=128)]


class HouseholdCalculationSubmission(StrictRouteModel):
    """One stored household under one policy, to calculate.

    ``household_id`` and ``policy_id`` (none for current law) name the
    Public API's rows of ``country``; ``policyengine_version`` selects the
    country's catalog version and the household function. The calculation
    is for the household's ``default_year``. A calculation submitted again
    reuses its run while it is unfinished or for 28 days after it succeeded,
    unless ``force``; after that, it runs again under a new calculation ID.
    """

    country: CountryId
    policyengine_version: PolicyEngineVersion
    household_id: UUID
    policy_id: UUID | None = None
    options: HouseholdCalculationOptions = Field(
        default_factory=HouseholdCalculationOptions
    )
    force: bool = False
    trigger: RunTrigger = RunTrigger.INITIAL

    def simulation_definition(self) -> NewSimulation:
        """Return the household simulation this calculation runs."""
        return household_simulation_definition(
            self.country, self.household_id, self.policy_id, self.options
        )


class HouseholdReportSubmission(StrictRouteModel):
    """A household report to create: a stored household under a baseline and a reform.

    ``reform`` is omitted for a report of the baseline alone; each policy is
    a stored policy's ID, or none for current law. Both are calculated with
    the same ``options``, for the household's ``default_year``, in one Modal
    call when neither has a run to reuse. The report's first run starts at
    once.
    """

    country: CountryId
    policyengine_version: PolicyEngineVersion
    household_id: UUID
    baseline: HouseholdPolicyChoice = Field(default_factory=HouseholdPolicyChoice)
    reform: HouseholdPolicyChoice | None = None
    options: HouseholdCalculationOptions = Field(
        default_factory=HouseholdCalculationOptions
    )
    force: bool = False
    trigger: RunTrigger = RunTrigger.INITIAL

    def policy_ids(self) -> list[UUID | None]:
        """Return the baseline's policy and, when there is a reform, its policy."""
        return [self.baseline.policy_id] + (
            [] if self.reform is None else [self.reform.policy_id]
        )

    def simulation_definition(self, policy_id: UUID | None) -> NewSimulation:
        """Return the household simulation of one of the report's policies."""
        return household_simulation_definition(
            self.country, self.household_id, policy_id, self.options
        )


class HouseholdReportRunSubmission(StrictRouteModel):
    """Another run of a stored household report, on a catalog version.

    The report's household, policies, options and year are read from the
    report; only the PolicyEngine.py version is chosen. The runs are reused
    while unfinished or for 28 days after they succeeded, unless ``force``.
    """

    policyengine_version: PolicyEngineVersion
    force: bool = False
    trigger: RunTrigger = RunTrigger.MANUAL


def household_simulation_definition(
    country: str,
    household_id: UUID,
    policy_id: UUID | None,
    options: HouseholdCalculationOptions,
) -> NewSimulation:
    """Return the household simulation of a household under a policy."""
    return NewSimulation(
        kind=SimulationKind.HOUSEHOLD,
        country=country,
        household_id=household_id,
        policy_id=policy_id,
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


class HouseholdRequestFieldError(BaseModel):
    """One request field FastAPI's validation refused, and why."""

    loc: list[str | int]
    msg: str
    type: str


class HouseholdRefusalResponse(BaseModel):
    """A refusal of the caller's data (422).

    ``detail`` is text when sim refuses the stored household or policy the
    request names, such as a parameter the catalog version lacks, a policy
    value or a household the household functions' contract refuses; and one
    entry per field when FastAPI's validation refuses the request's fields.
    """

    detail: str | list[HouseholdRequestFieldError]
