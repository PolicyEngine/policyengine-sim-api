"""Request and response models of the economy simulation and report routes.

The Public API names a region and dataset of the country's catalog by code
and name, and each simulation's stored policy and dynamic by ID; the
Simulation Entrypoint reads them from the Public API's tables itself, and
calculates on the v2 version manifest's default worker. A request names no
.py version and no record ID: the service runs its default worker's version
and chooses every ID.

Answers the Public API reads:

- 201 with the created record's ID and its first run's ID, or, for a
  re-run, the record's ID and its new run's ID;
- 400 with ``code`` and ``detail``: one of ``CallerRowRefusalCode`` when
  the region, dataset or a dynamic the request names (or, for a re-run, the
  stored record names) does not exist, or another code for the service's own
  state, such as a catalog version missing while a release deploys. Nothing
  is written;
- 404 ``unknown_report`` or ``unknown_simulation`` when a re-run names no
  stored record;
- 409 with ``code`` and ``detail``, ``rerun_not_supported``, for a stored
  record this service does not re-run. Nothing is written. No re-run is
  refused while a run is under way: an unforced one joins it, and a forced
  one is an extra run;
- 422 when the request's fields, or the stored values it names, cannot be
  calculated. Nothing is written;
- 503 with ``code`` and ``detail`` when the service cannot answer now. Only
  ``modal_call_refused`` follows a write, and it names the record created
  or re-run (``report_id`` or ``simulation_id``), whose new run was failed,
  and for a re-run that run (``run_id``);
- 500 with ``code`` ``internal_error``, possibly after a write: an
  unexpected failure, or a commit that failed after the rows were inserted.

``detail`` never holds an exception's text, and ``code`` is a fixed
identifier.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from policyengine_simulation_contract.spm import SPMSelection
from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_persistence import ReportRunStatus, SimulationStatus
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from pydantic.json_schema import SkipJsonSchema

# The bounds the tables accept.
SimulationYear = Annotated[int, Field(ge=1900, le=2200, description="The year")]
CatalogKey = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^\S+$")]
ReportLabel = Annotated[str, Field(min_length=1, max_length=255)]
ReportDescription = Annotated[str, Field(max_length=10_000)]
# A report's name and description are accepted and not stored: each user's
# saved report in the Public API holds them (C1 · 24's answer 6).
LABEL_NOT_STORED = (
    "The report's name, accepted for the Public API's callers and not stored: "
    "each user's saved report holds it."
)
DESCRIPTION_NOT_STORED = (
    "The report's description, accepted and not stored, as the label is."
)

# Why a request is refused (400), naming a row the caller chose that doesn't
# exist: the Public API answers these as its caller's error. On a re-run they
# name the stored record's own region, dataset or dynamic, which the current
# catalog version lacks or which was deleted.
CallerRowRefusalCode = Literal["unknown_region", "unknown_dataset", "unknown_dynamic"]
# Why a request is refused (400) for the service's own state, or a row the
# Public API checks before asking. On a report's re-run, ``unknown_policy`` and
# ``missing_reference`` name the stored report's own policy or simulation.
ServiceStateRefusalCode = Literal[
    # The country has no catalog version for the default worker's .py version.
    "unknown_catalog_version",
    "unknown_policy",
    # The default worker cannot calculate the country's economy runs.
    "no_economy_worker",
    # The default worker's bundle lacks the region's default dataset.
    "dataset_not_in_bundle",
    # The database refused a row: a row it names was deleted after the reads.
    "missing_reference",
    # A household report's household is not one of the country's.
    "unknown_household",
    # The default worker has no household function for the country.
    "no_household_function",
]
EconomyRefusalCode = Literal[CallerRowRefusalCode, ServiceStateRefusalCode]
# Why stored values cannot be calculated (422).
EconomyUnprocessableCode = Literal[
    "missing_parameters",
    "overlapping_parameters",
    # A household report's stored household has no default year or cannot
    # be read, or the household functions' contract refuses what the report
    # would calculate: located at the household or a side's policy.
    "household_not_calculable",
    # A report asks for cliffs (include_cliffs: true), which wait for each
    # simulation run's own cliff calculation (C1 · 39); located at the field.
    "cliffs_not_available",
]
# Why a stored record is not re-run now (409).
EconomyConflictCode = Literal[
    # The service does not re-run this kind of record, or the record lacks a
    # row it calculates on.
    "rerun_not_supported",
]
# Why a route cannot answer now (503); retry after ``Retry-After`` seconds.
EconomyUnavailableCode = Literal[
    # The service has no database or v2 Modal environment configured.
    "not_configured",
    "manifest_unavailable",
    "database_unavailable",
    # Modal refused the run's call, or did not accept it in time; the record
    # named was created and its run failed at once.
    "modal_call_refused",
]
# Every code an economy route's error answer carries.
EconomyErrorCode = Literal[
    EconomyRefusalCode,
    EconomyUnprocessableCode,
    EconomyConflictCode,
    EconomyUnavailableCode,
    "internal_error",
    "unknown_report",
    "unknown_simulation",
]


def omit_null_default(schema: dict[str, Any]) -> None:
    """Leave out a field's null default: the field is omitted rather than null."""
    schema.pop("default", None)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class PolicyChoice(_Request):
    """A simulation's stored policy and dynamic; neither means current law."""

    policy_id: UUID | None = None
    dynamic_id: UUID | None = None


class EconomySimulationRequest(PolicyChoice):
    """An economy simulation: a policy and dynamic applied to one region's data.

    ``region_code`` is a region of the country's catalog version for the
    default worker's .py version (``us``, ``state/ca``, ``country/wales``),
    and ``dataset_name`` one of that version's datasets; without it, the
    region's default dataset is used.
    """

    country: CountryId
    simulation_type: Literal["economy"]
    year: SimulationYear
    region_code: CatalogKey
    dataset_name: CatalogKey | None = None


class EconomyReportRequest(_Request):
    """An economy report: a baseline and a reform simulation of one region's data.

    Both simulations use the report's year, region and dataset, as
    ``EconomySimulationRequest`` describes them. ``include_cliffs`` also
    calculates the baseline's and reform's cliff impact. ``spm`` selects the
    SPM forecast scenario and geography; omit it, rather than sending null,
    to use the bundle's certified defaults.
    """

    country: CountryId
    report_type: Literal["economy"]
    label: ReportLabel | None = Field(default=None, description=LABEL_NOT_STORED)
    description: ReportDescription | None = Field(
        default=None, description=DESCRIPTION_NOT_STORED
    )
    year: SimulationYear
    region_code: CatalogKey
    dataset_name: CatalogKey | None = None
    baseline: PolicyChoice
    reform: PolicyChoice
    include_cliffs: StrictBool = False
    spm: SPMSelection | SkipJsonSchema[None] = Field(
        default=None, json_schema_extra=omit_null_default
    )

    @field_validator("spm", mode="before")
    @classmethod
    def refuse_null_spm(cls, value: object) -> object:
        """Refuse an explicit null; omit ``spm`` to send no settings."""
        if value is None:
            msg = "spm must be an object when given; omit it to send no settings"
            raise ValueError(msg)
        return value

    def options(self) -> dict[str, Any]:
        """Return the report's options as it stores them (``reports.inputs``).

        ``spm`` holds only the settings the request gave, and is left out
        when it gave none.
        """
        options: dict[str, Any] = {"include_cliffs": self.include_cliffs}
        if self.spm is not None:
            options["spm"] = self.spm.model_dump(mode="json")
        return options


class ReportRerunRequest(_Request):
    """A stored report's re-run on the default worker's versions.

    Without ``force``, a re-run of a report created by key (economy,
    budget window or household) joins the report's run on those versions
    that hasn't failed and wasn't forced, pending, running or succeeded,
    and otherwise each side joins its simulation's run on them or gets a
    new one, as a new report's side does. With it, and always for a report
    created before keys (C1 · 24's interim answer B), the report's
    simulations are calculated again, as an extra run marked forced that
    nothing joins.
    """

    force: StrictBool = False


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EconomySimulationCreated(_Answer):
    """The simulation a request created or re-ran, its run and that run's status.

    For a run the request created, the status is the run's when its call's
    ID was recorded: ``pending``, or whatever the simulation function had
    made it by then if it claimed the run first. A run whose call Modal
    didn't confirm, or whose call's ID couldn't be recorded, is answered
    ``pending``. A re-run that joined a run answers that run's status as
    read.
    """

    simulation_id: UUID
    simulation_run_id: UUID
    status: SimulationStatus


class EconomyReportCreated(_Answer):
    """The report a request ended on, its run and that run's status.

    The report and run are the ones a create found or created by key, or a
    re-run joined or created. For a run the request created, the status is
    the run's when its call's ID was recorded: ``pending``, or whatever the
    coordinator had made it by then if it claimed the run first. A run whose
    call Modal didn't confirm, or whose call's ID couldn't be recorded, is
    answered ``pending``. A joined run answers its status as read. Each side
    names its simulation and simulation run; ``…_reused`` is true for a side
    whose run this request didn't create: another request's run, in any
    state, that the side joined, or, on a joined report run, that run's own.
    """

    report_id: UUID
    report_run_id: UUID
    status: ReportRunStatus
    baseline_simulation_id: UUID
    baseline_simulation_run_id: UUID
    baseline_reused: bool
    reform_simulation_id: UUID
    reform_simulation_run_id: UUID
    reform_reused: bool


class SimulationRunRead(_Answer):
    """A simulation run's state: its status, versions, error message and times."""

    run_id: UUID
    status: SimulationStatus
    country_package_version: str
    policyengine_version: str
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class ReportRunRead(_Answer):
    """A report run's state, and the simulation runs it compares."""

    run_id: UUID
    status: ReportRunStatus
    country_package_version: str
    policyengine_version: str
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    baseline_simulation_run_id: UUID
    reform_simulation_run_id: UUID | None


class EconomyReportState(_Answer):
    """A report, its two simulations and its newest run, as they stand.

    The newest run is the one created last, ties going to the larger ID;
    ``run`` is null for a report with none. Its results are read from the
    Public API.
    """

    report_id: UUID
    country: str
    baseline_simulation_id: UUID | None
    reform_simulation_id: UUID | None
    run: ReportRunRead | None


class EconomySimulationState(_Answer):
    """A simulation and its newest run, as they stand; ``run`` null with none."""

    simulation_id: UUID
    run: SimulationRunRead | None


class EconomyError(_Answer):
    """Why an economy route refused a request or cannot answer now.

    ``report_id`` (on ``/reports``) or ``simulation_id`` (on
    ``/simulations``) is set only with ``modal_call_refused``: it names the
    record the service created or re-ran, whose new run it failed because
    Modal refused the call or didn't accept it in time. ``run_id`` names
    that new run on a re-run (a budget window's: its budget-window run).
    """

    code: EconomyErrorCode
    detail: str
    report_id: UUID | None = None
    simulation_id: UUID | None = None
    run_id: UUID | None = None


class EconomyRequestReason(BaseModel):
    """One reason a request cannot be calculated, as FastAPI writes a field error.

    ``loc`` starts with the request field the reason traces to. FastAPI's own
    field errors may also carry ``input`` and ``ctx``.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    loc: list[str | int]
    msg: str
    type: str


class EconomyRequestUnprocessable(_Answer):
    """A request that cannot be calculated (422), one reason per refused field.

    Request fields FastAPI refuses carry no ``code``. Stored values that
    cannot be calculated carry one: ``missing_parameters`` for a policy or
    dynamic setting parameters the catalog version lacks, and
    ``overlapping_parameters`` for a policy and dynamic setting the same one.
    """

    detail: list[EconomyRequestReason]
    code: EconomyUnprocessableCode | SkipJsonSchema[None] = Field(
        default=None, json_schema_extra=omit_null_default
    )
