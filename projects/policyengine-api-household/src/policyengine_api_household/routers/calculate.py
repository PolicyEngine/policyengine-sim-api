"""POST /{country_id}/calculate — v1 household API parity endpoint.

Router recovered (with adaptation) from PR #49 (commit 019b808):
libs/policyengine-api/src/policyengine_api/api/routers/calculate.py

Differences vs. PR #49, both in the direction of v1 household-api
parity (the golden corpus diffs against the real household API):
* The response wraps the household in the v1 envelope
  ``{"status", "message", "result", "policyengine_bundle"}`` instead of
  returning the bare household. ``policyengine_bundle.model_version``
  pins the country model package version for parity diffs.
* The household schema is selected by ``country_id`` instead of letting
  Pydantic guess among a union (the PR #49 union could silently coerce
  a US household into the generic model, dropping tax_units etc.).
* Calculation runs on the raw request dict (as v1 does), so the result
  echoes back exactly the entity groups the caller sent.
"""

import logging
from enum import Enum
from typing import Annotated, Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from policyengine_api_household.country import (
    get_country,
    get_model_version,
)
from policyengine_api_household.models.household import (
    HouseholdGeneric,
    HouseholdUK,
    HouseholdUS,
    example_household_input_us,
)
from policyengine_api_household.periods import (
    detect_period_warnings,
    validate_period_budgets,
    validate_period_keys,
)
from policyengine_api_household.utils.deprecated_inputs import (
    drop_deprecated_inputs,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Limits for reform-style "axes" scans. Axes multiply the computation
# cost by count_0 * count_1 * ... for each entry, so uncapped axes can
# be used to DoS the compute pool. Keep these conservative (they match
# the v1 household API's endpoints/household.py).
MAX_AXES_ENTRIES = 10
MAX_AXES_COUNT = 100


def _validate_axes(household_json: dict) -> None:
    """Validate the optional `axes` field's shape and size."""
    axes = household_json.get("axes")
    if axes is None:
        return

    if not isinstance(axes, list):
        raise ValueError("'axes' must be a list")

    if len(axes) > MAX_AXES_ENTRIES:
        raise ValueError(
            f"'axes' may contain at most {MAX_AXES_ENTRIES} entries; "
            f"got {len(axes)}"
        )

    for i, entry in enumerate(axes):
        for axis in _axes_entry_specs(entry, i):
            _validate_axis_name(axis, i)
            _validate_axis_count(axis, i)


def _axes_entry_specs(entry, index: int) -> list[dict]:
    # Each entry may itself be a list of axis specifications, which
    # supports nested/crossed scans in policyengine-core.
    axes = entry if isinstance(entry, list) else [entry]
    for axis in axes:
        if not isinstance(axis, dict):
            raise ValueError(
                f"'axes[{index}]' must be an object or list of objects"
            )
    return axes


def _validate_axis_count(axis: dict, index: int) -> None:
    count = axis.get("count")
    if count is None:
        return

    count_int = _parse_axis_count(count, index)
    if count_int < 1 or count_int > MAX_AXES_COUNT:
        raise ValueError(
            f"'axes[{index}].count' must be between 1 and "
            f"{MAX_AXES_COUNT}; got {count_int}"
        )


def _validate_axis_name(axis: dict, index: int) -> None:
    name = axis.get("name")
    if not isinstance(name, str) or name == "":
        raise ValueError(f"'axes[{index}].name' must be a non-empty string")


def _parse_axis_count(count, index: int) -> int:
    try:
        return int(count)
    except (TypeError, ValueError):
        raise ValueError(f"'axes[{index}].count' must be an integer")


class CountryId(str, Enum):
    us = "us"
    uk = "uk"


HOUSEHOLD_SCHEMAS: dict[str, type[HouseholdGeneric]] = {
    "us": HouseholdUS,
    "uk": HouseholdUK,
}


@router.post("/{country_id}/calculate")
async def calculate(
    country_id: CountryId,
    household: Annotated[
        dict[str, Any],
        Body(examples=[example_household_input_us], embed=True),
    ],
) -> JSONResponse:
    # Validate the payload against the country schema before reaching
    # the compute layer. Axes are stripped first (as v1 does): they are
    # not part of the household schema.
    schema = HOUSEHOLD_SCHEMAS[country_id.value]
    household_for_schema = {k: v for k, v in household.items() if k != "axes"}
    try:
        schema.model_validate(household_for_schema)
        _validate_axes(household)
    except ValidationError as e:
        return JSONResponse(
            content={
                "status": "error",
                "message": f"Invalid household payload: {e}",
            },
            status_code=400,
        )
    except ValueError as e:
        return JSONResponse(
            content={"status": "error", "message": str(e)},
            status_code=400,
        )

    country = get_country(country_id.value)

    # Strip deprecated inputs from a copy before period validation so
    # partners who still pass removed/renamed variables get a warning +
    # working response instead of a `VariableNotFoundError` HTTP 500.
    deprecated_inputs = drop_deprecated_inputs(household)
    household = deprecated_inputs.household
    deprecation_warnings = deprecated_inputs.warnings

    # Validate inbound period data before reaching the compute layer.
    try:
        validate_period_keys(household, country.tax_benefit_system)
        validate_period_budgets(household, country.tax_benefit_system)
    except ValueError as e:
        return JSONResponse(
            content={"status": "error", "message": str(e)},
            status_code=400,
        )

    # Detect partial monthly input + annual output combinations so partners
    # see a heads-up that some months will read the engine's fallback.
    period_warnings = detect_period_warnings(
        household, country.tax_benefit_system
    )

    try:
        result = country.calculate(household=household, reform=None)
    except Exception as e:  # pragma: no cover - defensive parity path
        logger.exception(e)
        return JSONResponse(
            content={
                "status": "error",
                "message": (f"Error calculating household under policy: {e}"),
            },
            status_code=500,
        )

    response_body: dict[str, Any] = {
        "status": "ok",
        "message": None,
        "result": result,
        "policyengine_bundle": {
            "model_version": get_model_version(country_id.value),
            "data_version": None,
            "dataset": None,
        },
    }

    warning_messages = [w.message for w in deprecation_warnings] + [
        w.message for w in period_warnings
    ]
    if warning_messages:
        # Serialize to strings on the wire (matching v1); the structured
        # dataclasses stay available for future structured consumers.
        response_body["warnings"] = warning_messages

    return JSONResponse(content=response_body, status_code=200)
