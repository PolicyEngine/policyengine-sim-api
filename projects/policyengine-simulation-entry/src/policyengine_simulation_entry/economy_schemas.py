"""Request and response bodies of the v2 economy report routes."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from policyengine_simulation_contract.job_progress import JobStage
from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from pydantic import BaseModel, ConfigDict, Field, JsonValue

CountryCode = Literal["us", "uk"]
CatalogKey = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^\S+$")]
PolicyEngineVersion = Annotated[str, Field(min_length=1, max_length=128)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SimulationChoiceRequest(_Request):
    """A simulation's policy and dynamic; neither means current law."""

    policy_id: UUID | None = None
    dynamic_id: UUID | None = None


class EconomyReportSubmissionRequest(_Request):
    """A new economy report and its first run.

    ``policyengine_version`` selects the country's catalog version.
    ``region_code`` is a region of that version (``state/ca``,
    ``country/wales``), and ``dataset_name`` one of its datasets; without it
    the region's default dataset is used. ``options`` must be empty for now.
    """

    country: CountryCode
    policyengine_version: PolicyEngineVersion
    year: Annotated[int, Field(ge=1900, le=2200)]
    region_code: CatalogKey
    dataset_name: CatalogKey | None = None
    baseline: SimulationChoiceRequest
    reform: SimulationChoiceRequest
    options: dict[str, JsonValue] = Field(default_factory=dict)


class EconomyReportRunRequest(_Request):
    """Run a report again on a catalog version.

    ``manual`` is a user's re-run and ``system`` a run the caller starts
    itself, such as the first opening of a migrated report. Existing
    simulation runs are reused unless ``force`` is set.
    """

    policyengine_version: PolicyEngineVersion
    trigger: Literal["manual", "system"]
    force: bool = False


class ReportStatus(StrEnum):
    """Where a report or report run stands; a report's is its newest run's."""

    NOT_RUN = "not_run"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SimulationResponse(BaseModel):
    """A simulation: one policy applied to one region."""

    id: UUID
    kind: str
    country: str
    policy_id: UUID | None
    dynamic_id: UUID | None
    region_code: str | None
    dataset_name: str | None
    legacy_simulation_id: int | None
    created_at: datetime


class RunErrorResponse(BaseModel):
    """Why a run failed, without user data."""

    code: str
    message: str | None


class SimulationRunResponse(BaseModel):
    """One execution of a simulation for a catalog version and year."""

    id: UUID
    simulation_id: UUID
    status: str
    attempt: int
    trigger: str
    error: RunErrorResponse | None
    dispatched_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class ComparisonResponse(BaseModel):
    """A report run's comparison step."""

    status: str
    error: RunErrorResponse | None
    dispatched_at: datetime | None
    completed_at: datetime | None


class EconomyReportRunResponse(BaseModel):
    """One calculation of a report: two simulation runs and their comparison.

    ``stage`` says what the run is doing while it runs: ``queued`` before
    any call has started, ``simulating`` while a simulation runs, and
    ``outputs`` while the comparison runs.
    """

    id: UUID
    report_id: UUID
    trigger: str
    catalog_version_id: UUID
    policyengine_version: str | None
    year: int
    status: ReportStatus
    stage: JobStage | None
    error: RunErrorResponse | None
    baseline_run: SimulationRunResponse
    reform_run: SimulationRunResponse
    comparison: ComparisonResponse
    created_at: datetime


class EconomyReportRunsResponse(BaseModel):
    """A report's runs, newest first."""

    runs: list[EconomyReportRunResponse]


class EconomyReportResponse(BaseModel):
    """An economy report, its newest run and, once compared, its result."""

    id: UUID
    country: str
    year: int
    options: dict[str, JsonValue]
    legacy_report_id: int | None
    created_at: datetime
    baseline_simulation: SimulationResponse
    reform_simulation: SimulationResponse
    status: ReportStatus
    run: EconomyReportRunResponse | None
    result: SingleYearMacroOutput | None
