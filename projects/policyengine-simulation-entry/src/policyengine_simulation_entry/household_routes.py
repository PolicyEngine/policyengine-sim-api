"""The household calculation routes: submit a calculation, then poll it.

- ``POST /v2/household-calculations`` (permission ``simulation:submit``)
  records the calculation and starts its Modal call, answering 202 with the
  calculation as it stands. Submitting the same calculation again returns
  the same one while it is unfinished or once it succeeded, unless forced.
- ``GET /v2/household-calculations/{calculation_id}`` (``simulation:read``)
  returns it: its status, and its household once it succeeded or its error
  once it failed. Poll until the status is ``succeeded`` or ``failed``.

Both answer 503 with ``Retry-After`` when household calculations are not
configured or a dependency cannot be reached. A submission that names a
version or country without a household function, or a household, policy or
catalog version the Public API does not have, gets 400.

This is v2 code: the main path's ``run_simulation`` does not use it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from policyengine_simulation_entry.household_api_models import (
    HouseholdCalculationResponse,
    HouseholdCalculationSubmission,
    HouseholdRouteError,
)
from policyengine_simulation_entry.household_calculations import (
    UnknownHouseholdReferenceError,
)
from policyengine_simulation_entry.household_workers import (
    HouseholdFunctionUnavailableError,
    HouseholdWorkersUnavailableError,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from policyengine_simulation_persistence import HouseholdCalculation

    from policyengine_simulation_entry.auth import AuthorizationDependency

logger = logging.getLogger(__name__)

CALCULATIONS_PATH = "/v2/household-calculations"
RETRY_AFTER_SECONDS = 10
NOT_CONFIGURED = "Household calculations are not configured."
WORKERS_UNAVAILABLE = "The v2 version manifest could not be read."
DATABASE_UNAVAILABLE = "The simulation database could not be reached."
UNAVAILABLE_RESPONSE = {
    "description": (
        "Household calculations are not configured, or a dependency could not "
        "be reached; retry after Retry-After seconds"
    ),
    "model": HouseholdRouteError,
}


class HouseholdCalculations(Protocol):
    """Submit and read household calculations (``HouseholdCalculationService``)."""

    async def submit(
        self, submission: HouseholdCalculationSubmission
    ) -> HouseholdCalculation:
        """Record the calculation, start its call if needed, and return it."""
        ...

    async def read(self, calculation_id: UUID) -> HouseholdCalculation | None:
        """Return the household calculation, or None if there is none."""
        ...


def unavailable(detail: str) -> JSONResponse:
    """Answer 503 with a wait before retrying."""
    return JSONResponse(
        HouseholdRouteError(detail=detail).model_dump(),
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


def refused(detail: str, status_code: int) -> JSONResponse:
    """Answer a refusal with its reason."""
    return JSONResponse(
        HouseholdRouteError(detail=detail).model_dump(), status_code=status_code
    )


def calculation_response(
    calculation: HouseholdCalculation, status_code: int
) -> JSONResponse:
    """Answer with the calculation as it stands."""
    return JSONResponse(
        HouseholdCalculationResponse.from_calculation(calculation).model_dump(
            mode="json"
        ),
        status_code=status_code,
    )


def register_household_calculation_routes(
    app: FastAPI,
    calculations: HouseholdCalculations | None,
    *,
    submitter: AuthorizationDependency,
    reader: AuthorizationDependency,
) -> None:
    """Add the household calculation routes; without a service they answer 503."""
    router = APIRouter()

    @router.post(
        CALCULATIONS_PATH,
        summary="Submit a household calculation",
        description=(
            "Record a household calculation (a household under one policy, for a "
            "catalog version and year) and start it. Poll the returned "
            "calculation_id until its status is succeeded or failed."
        ),
        operation_id="submit_household_calculation",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=HouseholdCalculationResponse,
        responses={
            400: {
                "description": (
                    "No household function for this version and country, or an "
                    "unknown household, policy or catalog version"
                ),
                "model": HouseholdRouteError,
            },
            503: UNAVAILABLE_RESPONSE,
        },
        dependencies=[Depends(submitter)],
    )
    async def submit_household_calculation(
        body: HouseholdCalculationSubmission,
    ) -> JSONResponse:
        if calculations is None:
            return unavailable(NOT_CONFIGURED)
        try:
            calculation = await calculations.submit(body)
        except (
            HouseholdFunctionUnavailableError,
            UnknownHouseholdReferenceError,
        ) as error:
            return refused(str(error), status.HTTP_400_BAD_REQUEST)
        except HouseholdWorkersUnavailableError:
            logger.warning("household_calculation_manifest_unavailable")
            return unavailable(WORKERS_UNAVAILABLE)
        except SQLAlchemyError as error:
            logger.warning(
                "household_calculation_database_unavailable",
                extra={"error_type": type(error).__name__},
            )
            return unavailable(DATABASE_UNAVAILABLE)
        return calculation_response(calculation, status.HTTP_202_ACCEPTED)

    @router.get(
        f"{CALCULATIONS_PATH}/{{calculation_id}}",
        summary="Get a household calculation",
        description=(
            "Return the calculation's status, and its household once it "
            "succeeded or its error once it failed."
        ),
        operation_id="get_household_calculation",
        response_model=HouseholdCalculationResponse,
        responses={
            404: {"description": "No such calculation", "model": HouseholdRouteError},
            503: UNAVAILABLE_RESPONSE,
        },
        dependencies=[Depends(reader)],
    )
    async def get_household_calculation(calculation_id: UUID) -> JSONResponse:
        if calculations is None:
            return unavailable(NOT_CONFIGURED)
        try:
            calculation = await calculations.read(calculation_id)
        except SQLAlchemyError as error:
            logger.warning(
                "household_calculation_database_unavailable",
                extra={"error_type": type(error).__name__},
            )
            return unavailable(DATABASE_UNAVAILABLE)
        if calculation is None:
            return refused("No such household calculation.", status.HTTP_404_NOT_FOUND)
        return calculation_response(calculation, status.HTTP_200_OK)

    app.include_router(router)
