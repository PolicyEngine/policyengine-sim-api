"""Household calculation outcomes, as a household function returns them.

The builders key each policy's result by its run (``policy_key_for_run``), as
the Simulation Entrypoint asks the household function to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionFailure,
    HouseholdExecutionResult,
)
from policyengine_simulation_contract.spm import (
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_provenance,
)
from policyengine_simulation_entry.household_recording import policy_key_for_run

if TYPE_CHECKING:
    from collections.abc import Sequence

CALCULATION_ID = UUID("00000000-0000-4000-8000-0000000000c1")
BUNDLE = {
    "policyengine_version": "6.10.0",
    "core_package_version": "3.33.0",
    "country_package_name": "policyengine-us",
    "country_package_version": "1.450.0",
    "bundle_manifest_sha256": "a" * 64,
}
WORKER = {
    "application_name": "policyengine-simulation-v2-py6-10-0",
    "function_name": "calculate_household_us",
}
SPM_CONFIG = {
    "forecast_content_sha256": "c" * 64,
    "scenario": "ce_trend",
    "geography_kind": "national",
    "geography_id": None,
    "county_vintage": "2020",
    "as_of": None,
}
DEPRECATED_INPUT_WARNING = "The input `age_group` is deprecated and was ignored."


def spm_receipt() -> dict[str, Any]:
    """Return the SPM receipt of a national, central-trend US calculation."""
    return build_spm_provenance(
        forecast_id="forecast-2026",
        forecast_sha256="c" * 64,
        selection=SPMSelection.model_validate(SPM_CONFIG),
        years=("2026",),
        runtime_versions=SPMRuntimeVersions.model_validate(
            {
                "policyengine": "6.10.0",
                "policyengine-core": "3.33.0",
                "policyengine-us": "1.450.0",
                "spm-calculator": "0.4.0",
            }
        ),
    ).model_dump(mode="json")


def calculated_household(net_income: float) -> dict[str, Any]:
    """Return a one-person US household with its net income calculated.

    Its group collection comes before ``people``, as a caller may send it.
    """
    return {
        "households": {
            "your household": {
                "members": ["you"],
                "household_net_income": {"2026": net_income},
            }
        },
        "people": {"you": {"age": {"2026": 40}}},
    }


def succeeded_outcome(run_ids: Sequence[UUID]) -> dict[str, Any]:
    """Return a successful calculation with one result per run, in order.

    Each run's household has a net income of 1000 times its position plus one.
    """
    return HouseholdExecutionResult.model_validate(
        {
            "calculation_id": CALCULATION_ID,
            "output": {
                "country": "us",
                "bundle": BUNDLE,
                "spm_config": SPM_CONFIG,
                "results": [
                    {
                        "policy_key": policy_key_for_run(run_id),
                        "household": calculated_household(1000.0 * (position + 1)),
                        "warnings": [DEPRECATED_INPUT_WARNING],
                        "spm_provenance": spm_receipt(),
                    }
                    for position, run_id in enumerate(run_ids)
                ],
            },
            "worker": WORKER,
        }
    ).model_dump(mode="json")


def failed_outcome() -> dict[str, Any]:
    """Return a calculation the household function rejected for unknown inputs."""
    return HouseholdExecutionFailure.model_validate(
        {
            "calculation_id": CALCULATION_ID,
            "error": {
                "code": "invalid_inputs",
                "message": "Unrecognized inputs: employment_incme",
                "input_errors": [
                    {
                        "type": "household_variable",
                        "name": "employment_incme",
                        "path": "people/you/employment_incme",
                        "message": "employment_incme is not a variable",
                    }
                ],
            },
            "worker": WORKER,
        }
    ).model_dump(mode="json")
