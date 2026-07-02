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

logger = logging.getLogger(__name__)

router = APIRouter()


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
    except ValidationError as e:
        return JSONResponse(
            content={
                "status": "error",
                "message": f"Invalid household payload: {e}",
            },
            status_code=400,
        )

    country = get_country(country_id.value)

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

    return JSONResponse(
        content={
            "status": "ok",
            "message": None,
            "result": result,
            "policyengine_bundle": {
                "model_version": get_model_version(country_id.value),
                "data_version": None,
                "dataset": None,
            },
        },
        status_code=200,
    )
