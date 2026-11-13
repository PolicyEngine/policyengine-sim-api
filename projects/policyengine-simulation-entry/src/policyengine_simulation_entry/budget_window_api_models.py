"""Request and answer models of a budget-window report on ``POST /reports``.

A budget-window report compares one region's baseline and reform in each
year of a window of 1 to 75 consecutive years, ending by 2200. The request
names the first year and the window's length, and the same region, dataset,
policies and SPM choice an economy report names (``EconomyReportRequest``);
it sends no cliff option, which a window doesn't calculate. The answers and
their error codes are an economy report's.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from policyengine_simulation_contract.spm import SPMSelection
from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_persistence import (
    LATEST_YEAR,
    MAX_BUDGET_WINDOW_YEARS,
    ReportRunStatus,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from policyengine_simulation_entry.economy_api_models import (
    CatalogKey,
    PolicyChoice,
    ReportDescription,
    ReportLabel,
    SimulationYear,
    omit_null_default,
)

BudgetWindowSize = Annotated[
    int,
    Field(
        ge=1,
        le=MAX_BUDGET_WINDOW_YEARS,
        description="How many years the window compares, from its first",
    ),
]


class BudgetWindowReportRequest(BaseModel):
    """A budget-window report: a baseline and a reform in each year of a window.

    ``year`` is the window's first year, and ``budget_window_size`` its
    length. Every year uses the request's region and its dataset, as an
    economy report does; no request names a dataset. ``spm`` selects the SPM
    forecast scenario and geography; omit it, rather than sending null, to
    use the bundle's certified defaults.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    country: CountryId
    report_type: Literal["budget_window"]
    label: ReportLabel
    description: ReportDescription | None = None
    year: SimulationYear
    budget_window_size: BudgetWindowSize
    region_code: CatalogKey
    baseline: PolicyChoice
    reform: PolicyChoice
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

    @model_validator(mode="after")
    def refuse_years_past_the_last(self) -> BudgetWindowReportRequest:
        """Refuse a window whose last year is after 2200."""
        if self.year + self.budget_window_size - 1 > LATEST_YEAR:
            msg = f"a budget window must end by {LATEST_YEAR}"
            raise ValueError(msg)
        return self

    @property
    def years(self) -> tuple[int, ...]:
        """Return the window's years, earliest first."""
        return tuple(range(self.year, self.year + self.budget_window_size))

    def options(self) -> dict[str, Any]:
        """Return the report's options as it stores them (``reports.inputs``).

        They are a single-year economy report's without cliffs, so the
        window's years and such reports reuse each other's runs. ``spm``
        holds only the settings the request gave.
        """
        options: dict[str, Any] = {"include_cliffs": False}
        if self.spm is not None:
            options["spm"] = self.spm.model_dump(mode="json")
        return options


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BudgetWindowYearCreated(_Answer):
    """One year of a created window: its report run and each side's run.

    ``…_reused`` is true for a side that names a run that already succeeded
    in that year, rather than a new one.
    """

    year: int
    report_run_id: UUID
    baseline_simulation_id: UUID
    baseline_simulation_run_id: UUID
    baseline_reused: bool
    reform_simulation_id: UUID
    reform_simulation_run_id: UUID
    reform_reused: bool


class BudgetWindowReportCreated(_Answer):
    """The budget-window report a request created, and its years' runs.

    ``report_run_id`` is the first year's report run, and ``status`` the
    window's calculation's: ``pending`` once its first years' calls were
    started, or when the start couldn't be recorded. ``years`` lists every
    year, earliest first.
    """

    report_id: UUID
    report_run_id: UUID
    budget_window_run_id: UUID
    status: ReportRunStatus
    years: list[BudgetWindowYearCreated]
