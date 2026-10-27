"""The household routes: submit a calculation or a report, then poll it.

- ``POST /v2/household-calculations`` (permission ``simulation:submit``)
  records the calculation and starts its Modal call, answering 202 with the
  calculation as it stands. Submitting the same calculation again returns
  the same one while it is unfinished or once it succeeded, unless forced.
- ``GET /v2/household-calculations/{calculation_id}`` (``simulation:read``)
  returns it: its status, and its household once it succeeded or its error
  once it failed. Poll until the status is ``succeeded`` or ``failed``.
- ``POST /v2/household-reports`` (``simulation:submit``) creates a household
  report, a baseline and an optional reform, and starts its first run;
  ``POST /v2/household-reports/{report_id}/runs`` starts another run of it;
  ``GET /v2/household-reports/{report_id}`` (``simulation:read``) returns it
  with its newest run and that run's two calculations.

Every route answers 503 with ``Retry-After`` when household calculations are
not configured or a dependency cannot be reached. A submission that names a
version or country without a household function, a household, policy or
catalog version the Public API does not have, or, for a report's run,
another household or other policies than the report's, gets 400.

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
    HouseholdReportResponse,
    HouseholdReportRunSubmission,
    HouseholdReportSubmission,
    HouseholdRouteError,
)
from policyengine_simulation_entry.household_calculations import (
    ReportPoliciesMismatchError,
    UnknownHouseholdReferenceError,
)
from policyengine_simulation_entry.household_workers import (
    HouseholdFunctionUnavailableError,
    HouseholdWorkersUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from fastapi import FastAPI
    from policyengine_simulation_persistence import (
        HouseholdCalculation,
        HouseholdReport,
    )

    from policyengine_simulation_entry.auth import AuthorizationDependency

logger = logging.getLogger(__name__)

CALCULATIONS_PATH = "/v2/household-calculations"
REPORTS_PATH = "/v2/household-reports"
RETRY_AFTER_SECONDS = 10
NOT_CONFIGURED = "Household calculations are not configured."
WORKERS_UNAVAILABLE = "The v2 version manifest could not be read."
DATABASE_UNAVAILABLE = "The simulation database could not be reached."
REFUSED_RESPONSE = {
    "description": (
        "No household function for this version and country; an unknown "
        "household, policy or catalog version; or, for a report's run, another "
        "household or other policies than the report's"
    ),
    "model": HouseholdRouteError,
}
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

    async def submit_report(
        self, submission: HouseholdReportSubmission
    ) -> HouseholdReport:
        """Create a household report and start its first run."""
        ...

    async def rerun_report(
        self, report_id: UUID, submission: HouseholdReportRunSubmission
    ) -> HouseholdReport | None:
        """Start another run of a stored report; None if there is none."""
        ...

    async def read_report(self, report_id: UUID) -> HouseholdReport | None:
        """Return a household report with its newest run, or None."""
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


async def guarded[T](operation: Awaitable[T]) -> T | JSONResponse:
    """Await ``operation``, answering a refusal or an unreachable dependency."""
    try:
        return await operation
    except (
        HouseholdFunctionUnavailableError,
        UnknownHouseholdReferenceError,
        ReportPoliciesMismatchError,
    ) as error:
        return refused(str(error), status.HTTP_400_BAD_REQUEST)
    except HouseholdWorkersUnavailableError:
        logger.warning("household_manifest_unavailable")
        return unavailable(WORKERS_UNAVAILABLE)
    except SQLAlchemyError as error:
        logger.warning(
            "household_database_unavailable",
            extra={"error_type": type(error).__name__},
        )
        return unavailable(DATABASE_UNAVAILABLE)


def report_response(report: HouseholdReport, status_code: int) -> JSONResponse:
    """Answer with the report and one of its runs, as they stand."""
    return JSONResponse(
        HouseholdReportResponse.from_report(report).model_dump(mode="json"),
        status_code=status_code,
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
        responses={400: REFUSED_RESPONSE, 503: UNAVAILABLE_RESPONSE},
        dependencies=[Depends(submitter)],
    )
    async def submit_household_calculation(
        body: HouseholdCalculationSubmission,
    ) -> JSONResponse:
        if calculations is None:
            return unavailable(NOT_CONFIGURED)
        calculation = await guarded(calculations.submit(body))
        if isinstance(calculation, JSONResponse):
            return calculation
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
        calculation = await guarded(calculations.read(calculation_id))
        if isinstance(calculation, JSONResponse):
            return calculation
        if calculation is None:
            return refused("No such household calculation.", status.HTTP_404_NOT_FOUND)
        return calculation_response(calculation, status.HTTP_200_OK)

    _add_report_routes(router, calculations, submitter=submitter, reader=reader)
    app.include_router(router)


NO_REPORT = "No such household report."
NOT_FOUND_RESPONSE = {"description": NO_REPORT, "model": HouseholdRouteError}


def _add_report_routes(
    router: APIRouter,
    calculations: HouseholdCalculations | None,
    *,
    submitter: AuthorizationDependency,
    reader: AuthorizationDependency,
) -> None:
    """Add the household report routes to ``router``."""
    for add_route in (_add_report_creation, _add_report_run):
        router.include_router(add_route(calculations, submitter))
    router.include_router(_add_report_read(calculations, reader))


def _add_report_creation(
    calculations: HouseholdCalculations | None, submitter: AuthorizationDependency
) -> APIRouter:
    report_router = APIRouter()

    @report_router.post(
        REPORTS_PATH,
        summary="Create a household report",
        description=(
            "Create a household report (the household under a baseline and an "
            "optional reform, for a year) and start its first run on a catalog "
            "version. Poll the report until its status is succeeded or failed."
        ),
        operation_id="submit_household_report",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=HouseholdReportResponse,
        responses={400: REFUSED_RESPONSE, 503: UNAVAILABLE_RESPONSE},
        dependencies=[Depends(submitter)],
    )
    async def submit_household_report(
        body: HouseholdReportSubmission,
    ) -> JSONResponse:
        if calculations is None:
            return unavailable(NOT_CONFIGURED)
        report = await guarded(calculations.submit_report(body))
        if isinstance(report, JSONResponse):
            return report
        return report_response(report, status.HTTP_202_ACCEPTED)

    return report_router


def _add_report_run(
    calculations: HouseholdCalculations | None, submitter: AuthorizationDependency
) -> APIRouter:
    report_router = APIRouter()

    @report_router.post(
        f"{REPORTS_PATH}/{{report_id}}/runs",
        summary="Run a household report again",
        description=(
            "Start another run of a stored household report on a catalog "
            "version, reusing runs that are unfinished or succeeded unless "
            "forced. The answer describes the new run."
        ),
        operation_id="run_household_report",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=HouseholdReportResponse,
        responses={
            400: REFUSED_RESPONSE,
            404: NOT_FOUND_RESPONSE,
            503: UNAVAILABLE_RESPONSE,
        },
        dependencies=[Depends(submitter)],
    )
    async def run_household_report(
        report_id: UUID, body: HouseholdReportRunSubmission
    ) -> JSONResponse:
        if calculations is None:
            return unavailable(NOT_CONFIGURED)
        report = await guarded(calculations.rerun_report(report_id, body))
        if isinstance(report, JSONResponse):
            return report
        if report is None:
            return refused(NO_REPORT, status.HTTP_404_NOT_FOUND)
        return report_response(report, status.HTTP_202_ACCEPTED)

    return report_router


def _add_report_read(
    calculations: HouseholdCalculations | None, reader: AuthorizationDependency
) -> APIRouter:
    report_router = APIRouter()

    @report_router.get(
        f"{REPORTS_PATH}/{{report_id}}",
        summary="Get a household report",
        description=(
            "Return the report with its newest run, that run's status, and its "
            "baseline and reform calculations."
        ),
        operation_id="get_household_report",
        response_model=HouseholdReportResponse,
        responses={404: NOT_FOUND_RESPONSE, 503: UNAVAILABLE_RESPONSE},
        dependencies=[Depends(reader)],
    )
    async def get_household_report(report_id: UUID) -> JSONResponse:
        if calculations is None:
            return unavailable(NOT_CONFIGURED)
        report = await guarded(calculations.read_report(report_id))
        if isinstance(report, JSONResponse):
            return report
        if report is None:
            return refused(NO_REPORT, status.HTTP_404_NOT_FOUND)
        return report_response(report, status.HTTP_200_OK)

    return report_router
