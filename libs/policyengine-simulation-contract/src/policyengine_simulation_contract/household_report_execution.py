"""The calculations of one household report run, as one household function takes them.

A household report calculates one stored household under a baseline policy
and, optionally, a reform. The simulation service creates a household
simulation run for each side, and a report run naming them, then starts one
household function call with the report run's ID and this document. Each
side is a ``HouseholdExecutionRequest`` whose ``calculation_id`` is the
simulation run it records; both sides calculate the same household, for the
same year, the same way, on the same bundle, and differ only in their
policy.
"""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from policyengine_simulation_contract.household_execution import (
    HOUSEHOLD_CONTRACT_SCHEMA_VERSION,
    HouseholdExecutionRequest,
    StrictHouseholdModel,
)

# Which side of a household report a calculation is.
HouseholdReportSide = Literal["baseline", "reform"]
# The request fields both sides share: everything but the policy and the
# simulation run each side records.
_SHARED_FIELDS: set[str] = set(HouseholdExecutionRequest.model_fields) - {
    "calculation_id",
    "policy",
}


class HouseholdReportRunCalculation(StrictHouseholdModel):
    """A household report run's baseline and, optionally, reform calculation.

    ``reform`` is None for a report of the baseline alone.
    """

    schema_version: Literal[1] = HOUSEHOLD_CONTRACT_SCHEMA_VERSION
    baseline: HouseholdExecutionRequest
    reform: HouseholdExecutionRequest | None = None

    @model_validator(mode="after")
    def validate_one_household(self) -> HouseholdReportRunCalculation:
        """Require two sides to record different runs of one household calculation."""
        if self.reform is None:
            return self
        if self.reform.calculation_id == self.baseline.calculation_id:
            msg = "the baseline and the reform must record different simulation runs"
            raise ValueError(msg)
        baseline = self.baseline.model_dump(include=_SHARED_FIELDS)
        reform = self.reform.model_dump(include=_SHARED_FIELDS)
        differing = sorted(
            name for name in _SHARED_FIELDS if baseline.get(name) != reform.get(name)
        )
        if differing:
            msg = (
                "the baseline and the reform must calculate the same household "
                f"the same way; they differ in {', '.join(differing)}"
            )
            raise ValueError(msg)
        return self

    def sides(
        self,
    ) -> tuple[tuple[HouseholdReportSide, HouseholdExecutionRequest], ...]:
        """Return each side's request, the baseline's first."""
        if self.reform is None:
            return (("baseline", self.baseline),)
        return (("baseline", self.baseline), ("reform", self.reform))
