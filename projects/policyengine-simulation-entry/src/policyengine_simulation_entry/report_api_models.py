"""The body of ``POST /reports``: an economy, budget-window or household report.

The three are told apart by ``report_type``; each model's own module
describes its fields and its answer.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from policyengine_simulation_entry.budget_window_api_models import (
    BudgetWindowReportRequest,
)
from policyengine_simulation_entry.economy_api_models import EconomyReportRequest
from policyengine_simulation_entry.household_report_api_models import (
    HouseholdReportRequest,
)

ReportRequest = Annotated[
    EconomyReportRequest | BudgetWindowReportRequest | HouseholdReportRequest,
    Field(discriminator="report_type"),
]
