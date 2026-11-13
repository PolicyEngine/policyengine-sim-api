"""Request and answer models of a household report on ``POST /reports``.

A household report calculates one household the Public API stores, for the
household's ``default_year``, under a baseline policy and, optionally, a
reform, each a stored policy or current law. The Simulation Entrypoint reads
the household and the policies from the Public API's tables itself and
calculates on the v2 version manifest's default worker; a request names no
.py version, no year and no record ID. Its answers and their error codes
are an economy report's (``economy_api_models``), with the household codes
``unknown_household``, ``no_household_function`` and
``household_not_calculable``.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_persistence import ReportRunStatus
from pydantic import BaseModel, ConfigDict

from policyengine_simulation_entry.economy_api_models import (
    ReportDescription,
    ReportLabel,
)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class HouseholdPolicyChoice(_Request):
    """A side's stored policy; null or omitted means current law."""

    policy_id: UUID | None = None


class HouseholdReportRequest(_Request):
    """A household report: one stored household under a baseline and a reform.

    ``reform`` is null, or omitted, for a report of the baseline alone. The
    report calculates the household's ``default_year``. Each side's
    calculation adds every yearly, monthly and eternal variable the
    household lacks, as the Public API's stored-household route does, so the
    report holds every value; a household report has no option for it. A
    household's saved SPM selection is applied, or the bundle's certified
    defaults without one.
    """

    country: CountryId
    report_type: Literal["household"]
    label: ReportLabel
    description: ReportDescription | None = None
    household_id: UUID
    baseline: HouseholdPolicyChoice
    reform: HouseholdPolicyChoice | None = None

    def options(self) -> dict[str, Any]:
        """Return the report's options as it stores them (``reports.inputs``).

        A household report has none: the column must hold an object.
        """
        return {}


class HouseholdReportCreated(BaseModel):
    """The household report a request created or re-ran, its new run and its status.

    The status is the run's when its call's ID was recorded: ``pending``, or
    whatever the household function had made it by then if it claimed the
    run first. A run whose call Modal didn't confirm, or whose call's ID
    couldn't be recorded, is answered ``pending``. ``year`` is the year the
    report calculates: a new report's is its household's default year, and a
    re-run keeps the report's. Each side names its simulation, new for a new
    report and the report's own on a re-run, and its new simulation run; the
    reform's are null for a report of the baseline alone.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: UUID
    report_run_id: UUID
    status: ReportRunStatus
    year: int
    baseline_simulation_id: UUID
    baseline_simulation_run_id: UUID
    reform_simulation_id: UUID | None
    reform_simulation_run_id: UUID | None
