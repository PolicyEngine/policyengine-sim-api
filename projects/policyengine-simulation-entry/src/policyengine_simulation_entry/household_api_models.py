"""Request and response models of the household calculation routes.

The Public API names a stored household, and optionally a stored policy, by
ID; the Simulation Entrypoint reads them from the Public API's tables itself
and calculates the household's ``default_year`` on the v2 version manifest's
default worker. Each submission creates a ``household_jobs`` row, which a
household function on Modal calculates; the read route returns it as it
stands.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from policyengine_simulation_contract.household_execution import Axes
from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_persistence import HouseholdJobStatus
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator
from pydantic.json_schema import SkipJsonSchema

# Why a submission is refused (400): a row it names, or the household
# function it needs, does not exist.
HouseholdRefusalCode = Literal[
    # The country has no catalog version for the default worker's .py version.
    "unknown_catalog_version",
    "unknown_household",
    "unknown_policy",
    # The default worker has no household function for the country.
    "no_household_function",
    # The database refused the job: a row it names was deleted after the reads.
    "missing_reference",
]
# Why a route cannot answer now (503); retry after ``Retry-After`` seconds.
HouseholdUnavailableCode = Literal[
    # The service has no database or v2 Modal environment configured.
    "not_configured",
    "manifest_unavailable",
    "database_unavailable",
    # Modal refused the household function's call, or did not accept it in
    # time; the job was failed at once.
    "modal_call_refused",
]
# Every code a household route's error answer carries.
HouseholdErrorCode = Literal[
    HouseholdRefusalCode, HouseholdUnavailableCode, "unknown_household_job"
]


def _omit_spm_default(schema: dict[str, Any]) -> None:
    """Leave out the null default: omit ``spm`` to send no settings."""
    schema.pop("default", None)


class HouseholdCalculationRequest(BaseModel):
    """A stored household to calculate under one stored policy, or current law.

    ``household_id`` and ``policy_id`` (omitted or null for current law) name
    the Public API's rows of ``country``. The calculation is for the
    household's ``default_year``, on the v2 version manifest's default worker.
    ``add_missing_variables`` adds every yearly, monthly and eternal variable
    the household lacks, as the Public API's ``/calculate-full`` does.
    ``axes`` vary the stored household's inputs across points, within the
    household contract's limits.

    ``spm`` holds SPM settings as an object, which the household function
    checks with the Public API's SPM error codes. Omit it to use the
    household's saved selection, or, without one, the bundle's certified
    defaults; an explicit null is refused.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    country: CountryId
    household_id: UUID
    policy_id: UUID | None = None
    add_missing_variables: bool = False
    axes: Axes = None
    spm: dict[str, JsonValue] | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "SPM settings as the household contract takes them, as an object. "
            "Omit to use the household's saved selection or the bundle's "
            "defaults; null is refused."
        ),
        json_schema_extra=_omit_spm_default,
    )

    @field_validator("spm", mode="before")
    @classmethod
    def refuse_null_spm(cls, value: object) -> object:
        """Refuse an explicit null; omit ``spm`` to send no settings."""
        if value is None:
            msg = "spm must be an object when given; omit it to send no settings"
            raise ValueError(msg)
        return value

    @property
    def spm_requested(self) -> bool:
        """Whether the request sent SPM settings, rather than leaving them out."""
        return "spm" in self.model_fields_set


class HouseholdCalculationAccepted(BaseModel):
    """The household job a submission created, and its status as it stands.

    Poll ``GET /household-calculations/{job_id}`` until the status is
    ``succeeded`` or ``failed``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    status: HouseholdJobStatus


class HouseholdCalculationState(BaseModel):
    """A household job as it stands.

    ``result`` is the household function's outcome document once it has
    finished, as the household contract defines it: a
    ``HouseholdExecutionResult`` (``status`` ``succeeded``, with the
    calculated household in ``output``) or a ``HouseholdExecutionFailure``
    (``status`` ``failed``, with the reason in ``error``). ``error_message``
    summarizes a failure in one line; a job that failed before its function
    ran has an ``error_message`` and no ``result``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    status: HouseholdJobStatus
    country: str
    household_id: UUID
    policy_id: UUID | None
    year: int
    policyengine_version: str
    country_package_version: str
    result: dict[str, JsonValue] | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class HouseholdCalculationError(BaseModel):
    """Why a household route refused a request or cannot answer now.

    ``household_job_id`` names the job a refused Modal call failed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: HouseholdErrorCode
    detail: str
    household_job_id: UUID | None = None


class HouseholdRequestReason(BaseModel):
    """One reason a request cannot be calculated, as FastAPI writes a field error.

    ``loc`` starts with the request field the reason traces to. FastAPI's own
    field errors may also carry ``input`` and ``ctx``.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    loc: list[str | int]
    msg: str
    type: str


class HouseholdRequestUnprocessable(BaseModel):
    """A request that cannot be calculated (422), one reason per refused field.

    Request fields FastAPI refuses, and stored data that cannot be
    calculated, share this shape: a household without a default year, a
    policy setting parameters the catalog version lacks, or a household or
    policy value the household contract refuses.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: list[HouseholdRequestReason]
