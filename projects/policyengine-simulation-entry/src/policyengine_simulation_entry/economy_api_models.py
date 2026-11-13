"""Request and response models of the economy simulation and report routes.

The Public API names a region and dataset of the country's catalog by code
and name, and each simulation's stored policy and dynamic by ID; the
Simulation Entrypoint reads them from the Public API's tables itself, and
calculates on the v2 version manifest's default worker. A request names no
.py version and no record ID: the service runs its default worker's version
and chooses every ID.

Answers the Public API reads:

- 201 with the created record's ID and its first run's ID;
- 400 with ``code`` and ``detail``: one of ``CallerRowRefusalCode`` when
  the region, dataset or a dynamic the request names does not exist, or
  another code for the service's own state, such as a catalog version
  missing while a release deploys. Nothing is written;
- 422 when the request's fields, or the stored values it names, cannot be
  calculated. Nothing is written;
- 503 with ``code`` and ``detail`` when the service cannot answer now. Only
  ``modal_call_refused`` follows a write, and it names the record created
  (``report_id`` or ``simulation_id``), whose run was failed;
- 500 with ``code`` ``internal_error``, possibly after a write: an
  unexpected failure, or a commit that failed after the rows were inserted.

``detail`` never holds an exception's text, and ``code`` is a fixed
identifier.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from policyengine_simulation_contract.local_area_breakdown import UK_NATION_REGIONS
from policyengine_simulation_contract.spm import SPMSelection
from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_persistence import ReportRunStatus, SimulationStatus
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationInfo,
    field_validator,
)
from pydantic.json_schema import SkipJsonSchema

# The bounds the tables accept.
SimulationYear = Annotated[int, Field(ge=1900, le=2200, description="The year")]
CatalogKey = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^\S+$")]
ReportLabel = Annotated[str, Field(min_length=1, max_length=255)]
ReportDescription = Annotated[str, Field(max_length=10_000)]
# The one data source a report may ask for: the bundle's local-area dataset,
# on which a UK-wide or nation report also calculates its constituency and
# local-authority impacts.
LOCAL_AREA_DATA_SOURCE = "local_area_dataset"
LocalAreaDataSource = Literal["local_area_dataset"]
# The regions whose reports may run on the local-area dataset.
LOCAL_AREA_DATA_SOURCE_REGIONS = frozenset({"uk", *UK_NATION_REGIONS})

# Why a request is refused (400), naming a row the caller chose that doesn't
# exist: the Public API answers these as its caller's error.
CallerRowRefusalCode = Literal["unknown_region", "unknown_dataset", "unknown_dynamic"]
# Why a request is refused (400) for the service's own state, or a row the
# Public API checks before asking.
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
    # The default worker's coordinator runs no local-area report runs of the
    # input this service sends.
    "local_areas_unsupported",
    # The catalog version's constituency and local-authority regions don't
    # all use the dataset the default worker calculates local areas on.
    "local_area_dataset_mismatch",
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
    # The default worker's .py release routes constituencies and local
    # authorities to no dataset of their own, so it has no local-area dataset
    # for ``data_source``: located at ``data_source``.
    "no_local_area_dataset",
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

    ``data_source: "local_area_dataset"`` runs the report on the bundle's
    local-area dataset, the one its .py release routes constituencies and
    local authorities to, in place of the region's own: the report returns
    its full results, cliffs included when asked, and its constituency and
    local-authority impacts. Only a UK report whose region is ``uk`` or one
    of the four ``country/*`` nations may ask; omit it, rather than sending
    null, to use the region's dataset. It is never stored as an option: the
    dataset it chooses is stored with the report's simulations.
    """

    country: CountryId
    report_type: Literal["economy"]
    label: ReportLabel
    description: ReportDescription | None = None
    year: SimulationYear
    region_code: CatalogKey
    dataset_name: CatalogKey | None = None
    baseline: PolicyChoice
    reform: PolicyChoice
    include_cliffs: StrictBool = False
    spm: SPMSelection | SkipJsonSchema[None] = Field(
        default=None, json_schema_extra=omit_null_default
    )
    # Last, so its check can read the country and region.
    data_source: LocalAreaDataSource | SkipJsonSchema[None] = Field(
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

    @field_validator("data_source", mode="before")
    @classmethod
    def refuse_another_data_source(cls, value: object) -> object:
        """Refuse null and any other value with one reason.

        Omit ``data_source`` to use the region's dataset. Without this,
        another value would be refused once by each member of the field's
        type.
        """
        if value != LOCAL_AREA_DATA_SOURCE:
            msg = (
                f'data_source must be "{LOCAL_AREA_DATA_SOURCE}" when given; omit '
                "it to use the region's dataset"
            )
            raise ValueError(msg)
        return value

    @field_validator("data_source")
    @classmethod
    def refuse_a_data_source_the_region_cannot_have(
        cls, data_source: LocalAreaDataSource | None, info: ValidationInfo
    ) -> LocalAreaDataSource | None:
        """Refuse the local-area dataset for another country or region.

        Only a UK report whose region is UK-wide or a nation may run on it. A
        country or region that failed its own validation is reported by that
        field alone.
        """
        country = info.data.get("country")
        region_code = info.data.get("region_code")
        if data_source is None or country is None or region_code is None:
            return data_source
        if country != "uk":
            msg = "the local-area dataset is available only to UK reports"
            raise ValueError(msg)
        if region_code not in LOCAL_AREA_DATA_SOURCE_REGIONS:
            accepted = ", ".join(sorted(LOCAL_AREA_DATA_SOURCE_REGIONS))
            msg = (
                "the local-area dataset needs a UK-wide or nation region "
                f"({accepted}), not {region_code!r}"
            )
            raise ValueError(msg)
        return data_source

    def options(self) -> dict[str, Any]:
        """Return the report's options as it stores them (``reports.inputs``).

        ``spm`` holds only the settings the request gave, and is left out
        when it gave none. ``data_source`` is never an option: the dataset
        it chooses is stored with the report's simulations.
        """
        options: dict[str, Any] = {"include_cliffs": self.include_cliffs}
        if self.spm is not None:
            options["spm"] = self.spm.model_dump(mode="json")
        return options


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EconomySimulationCreated(_Answer):
    """The simulation a request created, its first run and that run's status.

    The status is the run's when its call's ID was recorded: ``pending``, or
    whatever the simulation function had made it by then if it claimed the
    run first. A run whose call Modal didn't confirm, or whose call's ID
    couldn't be recorded, is answered ``pending``.
    """

    simulation_id: UUID
    simulation_run_id: UUID
    status: SimulationStatus


class EconomyReportCreated(_Answer):
    """The report a request created, its first run and that run's status.

    The status is the run's when its call's ID was recorded: ``pending``, or
    whatever the coordinator had made it by then if it claimed the run
    first. A run whose call Modal didn't confirm, or whose call's ID couldn't
    be recorded, is answered ``pending``. Each side names its
    simulation and simulation run; ``…_reused`` is true for a side that
    names a run that already succeeded, rather than a new one.
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
    record the service created, whose run it failed because Modal refused
    the call or didn't accept it in time.
    """

    code: EconomyErrorCode
    detail: str
    report_id: UUID | None = None
    simulation_id: UUID | None = None


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
    ``no_local_area_dataset`` refuses ``data_source`` on a .py release that
    routes constituencies and local authorities to no dataset of their own:
    its national reports calculate both area levels themselves.
    """

    detail: list[EconomyRequestReason]
    code: EconomyUnprocessableCode | SkipJsonSchema[None] = Field(
        default=None, json_schema_extra=omit_null_default
    )
