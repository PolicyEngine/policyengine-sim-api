"""Request and response bodies of the v2 budget-window report routes.

A budget-window report's result is today's budget-window format
(``BudgetWindowResult``): each year's budget figures and their totals, which
are summed when the report is read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Self
from uuid import UUID

from policyengine_simulation_contract.gateway_models import BudgetWindowResult
from policyengine_simulation_contract.job_progress import JobStage
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from policyengine_simulation_entry.economy_schemas import (
    CatalogKey,
    CountryCode,
    PolicyEngineVersion,
    ReportStatus,
    RunErrorResponse,
    SimulationChoiceRequest,
    SimulationResponse,
)

# A window may end no later than this year, as the old gateway's budget
# windows may, and have at most this many years.
LAST_WINDOW_YEAR = 2099
MAX_WINDOW_YEARS = 75


class BudgetWindowReportSubmissionRequest(BaseModel):
    """A new budget-window report and its first run.

    The report compares ``baseline`` and ``reform`` in each of
    ``window_size`` years from ``start_year``, ending in 2099 or earlier.
    The other fields are an economy report's: ``policyengine_version``
    selects the country's catalog version, ``region_code`` and
    ``dataset_name`` name its region and dataset, and ``options`` must be
    empty for now.
    """

    model_config = ConfigDict(extra="forbid")

    country: CountryCode
    policyengine_version: PolicyEngineVersion
    start_year: Annotated[int, Field(ge=1900, le=LAST_WINDOW_YEAR)]
    window_size: Annotated[int, Field(ge=1, le=MAX_WINDOW_YEARS)]
    region_code: CatalogKey
    dataset_name: CatalogKey | None = None
    baseline: SimulationChoiceRequest
    reform: SimulationChoiceRequest
    options: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _window_ends_in_time(self) -> Self:
        if self.start_year + self.window_size - 1 > LAST_WINDOW_YEAR:
            msg = f"a budget window must end in {LAST_WINDOW_YEAR} or earlier"
            raise ValueError(msg)
        return self


class BudgetWindowYearResponse(BaseModel):
    """One year of a budget-window run.

    A year the run has started has its report run, with that run's status,
    its stage while it runs (``queued``, ``simulating`` or ``outputs``) and
    its error. A year not started yet is ``pending`` and ``queued``; one a
    finished run never started is ``not_run``.
    """

    year: int
    status: ReportStatus
    stage: JobStage | None
    error: RunErrorResponse | None
    report_run_id: UUID | None


class BudgetWindowRunResponse(BaseModel):
    """One calculation of a budget-window report on a catalog version.

    ``status`` is ``failed`` as soon as one year has failed, ``succeeded``
    once every year has, ``running`` while any year is calculated, and
    ``pending`` before then. ``progress`` is the share of the window's years
    that have succeeded, from 0 to 100. At most ``max_concurrent_years`` are
    calculated at once; ``force`` calculates every year's simulations again.
    """

    id: UUID
    report_id: UUID
    trigger: str
    catalog_version_id: UUID
    policyengine_version: str | None
    force: bool
    max_concurrent_years: int
    status: ReportStatus
    error: RunErrorResponse | None
    progress: int
    years: list[BudgetWindowYearResponse]
    created_at: datetime
    completed_at: datetime | None


class BudgetWindowRunsResponse(BaseModel):
    """A budget-window report's runs, newest first."""

    runs: list[BudgetWindowRunResponse]


class BudgetWindowReportResponse(BaseModel):
    """A budget-window report, its newest run and, once it succeeded, its result."""

    id: UUID
    country: str
    start_year: int
    window_size: int
    options: dict[str, JsonValue]
    created_at: datetime
    baseline_simulation: SimulationResponse
    reform_simulation: SimulationResponse
    status: ReportStatus
    run: BudgetWindowRunResponse | None
    result: BudgetWindowResult | None
