"""The household calculation routes: submit a calculation, then read its job.

- ``POST /household-calculations`` (permission ``simulation:submit``) creates
  a household job and starts its household function's call, answering 202
  with the job's ID and status.
- ``GET /household-calculations/{household_job_id}`` (``simulation:read``)
  returns the job as it stands: its status, the household function's result
  document once it has one, and its error message.

A refused submission answers 400 (a row it names, or the household function
it needs, is missing) or 422 (its fields, or the stored data it names, cannot
be calculated), naming why. Either route answers 503 with ``Retry-After``
when household calculations are not configured or a dependency cannot be
read, and a submission does too when Modal refuses the call.

A submission gets its observability ID from the caller's header, or a new
one, and passes it to the household function. ``household_jobs`` keeps no
observability ID, so a read returns none.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from policyengine_simulation_entry.household_api_models import (
    HouseholdCalculationAccepted,
    HouseholdCalculationError,
    HouseholdCalculationRequest,
    HouseholdCalculationState,
    HouseholdRequestUnprocessable,
)
from policyengine_simulation_entry.household_calculations import (
    HouseholdCalculationRefusedError,
    HouseholdCalculationUnavailableError,
    HouseholdCalculationUnprocessableError,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from policyengine_simulation_persistence import HouseholdJobRecord

    from policyengine_simulation_entry.auth import AuthorizationDependency
    from policyengine_simulation_entry.entry_runtime import EntryRuntime

SUBMISSION_PATH = "/household-calculations"
JOB_PATH = "/household-calculations/{household_job_id}"
RETRY_AFTER_SECONDS = 10

_ERROR = {"model": HouseholdCalculationError}
_UNPROCESSABLE = {
    "description": (
        "The request's fields, or the stored household or policy it names, "
        "cannot be calculated: one reason per field, its location starting "
        "with the request field it traces to"
    ),
    "model": HouseholdRequestUnprocessable,
}
_UNAVAILABLE = {
    "description": (
        "Household calculations are not configured, the v2 version manifest "
        "or the database cannot be read, or Modal refused the household "
        "function's call (the job named by household_job_id was failed); "
        "retry after Retry-After seconds"
    ),
    **_ERROR,
}
_FORBIDDEN = {
    "description": "The caller's token is missing, invalid or lacks the permission"
}


class HouseholdCalculations(Protocol):
    """Submit and read household calculations (``HouseholdCalculationService``)."""

    async def submit(
        self,
        request: HouseholdCalculationRequest,
        *,
        observability_context: dict[str, Any] | None,
    ) -> HouseholdCalculationAccepted:
        """Create the household job and start its call."""
        ...

    async def read(self, job_id: UUID) -> HouseholdJobRecord | None:
        """Return the household job as it stands, or None."""
        ...


def _error_response(status_code: int, error: HouseholdCalculationError) -> JSONResponse:
    headers = (
        {"Retry-After": str(RETRY_AFTER_SECONDS)}
        if status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        else None
    )
    return JSONResponse(
        error.model_dump(mode="json", exclude_none=True),
        status_code=status_code,
        headers=headers,
    )


def _not_configured() -> JSONResponse:
    return _error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        HouseholdCalculationError(
            code="not_configured",
            detail="Household calculations are not configured.",
        ),
    )


def _unavailable(error: HouseholdCalculationUnavailableError) -> JSONResponse:
    return _error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        HouseholdCalculationError(
            code=error.code,
            detail=error.detail,
            household_job_id=error.household_job_id,
        ),
    )


def _observability_context(
    entry: EntryRuntime, observability_id: str
) -> dict[str, Any]:
    """Capture the trace context for the household function, with the ID.

    Telemetry is non-fatal: without a captured context, only the ID is sent.
    """
    captured: object = None
    with contextlib.suppress(Exception):
        captured = entry.runtime.capture_context()
    context = dict(captured) if isinstance(captured, dict) else {}
    context["observability_id"] = observability_id
    return context


def _state(record: HouseholdJobRecord) -> HouseholdCalculationState:
    return HouseholdCalculationState(
        job_id=record.id,
        status=record.status,
        country=record.country,
        household_id=record.household_id,
        policy_id=record.policy_id,
        year=record.year,
        policyengine_version=record.policyengine_version,
        country_package_version=record.country_package_version,
        result=record.result,
        error_message=record.error_message,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
    )


def add_household_calculation_routes(
    app: FastAPI,
    entry: EntryRuntime,
    calculations: HouseholdCalculations | None,
    *,
    submitter: AuthorizationDependency,
    reader: AuthorizationDependency,
) -> None:
    """Add the household routes; without a service they answer 503."""
    router = APIRouter()

    @router.post(
        SUBMISSION_PATH,
        summary="Submit a household calculation",
        description=(
            "Create a household job calculating a stored household under one "
            "stored policy, or current law, for the household's default year, "
            "on the v2 version manifest's default worker, and start it. Poll "
            "the returned id until its status is succeeded or failed."
        ),
        operation_id="submit_household_calculation",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=HouseholdCalculationAccepted,
        responses={
            202: {"description": "The job was created and its call started"},
            400: {
                "description": (
                    "A row the request names, or the household function it "
                    "needs, is missing; code says which"
                ),
                **_ERROR,
            },
            403: _FORBIDDEN,
            422: _UNPROCESSABLE,
            503: _UNAVAILABLE,
        },
        dependencies=[Depends(submitter)],
    )
    async def submit_household_calculation(
        body: HouseholdCalculationRequest, request: Request
    ) -> JSONResponse:
        if calculations is None:
            return _not_configured()
        observability_id = entry.start_workflow_observability(request)
        try:
            accepted = await calculations.submit(
                body,
                observability_context=_observability_context(entry, observability_id),
            )
        except HouseholdCalculationRefusedError as error:
            return _error_response(
                status.HTTP_400_BAD_REQUEST,
                HouseholdCalculationError(code=error.code, detail=error.detail),
            )
        except HouseholdCalculationUnprocessableError as error:
            return JSONResponse(
                {
                    "detail": [
                        {
                            "loc": list(reason.loc),
                            "msg": reason.msg,
                            "type": reason.type,
                        }
                        for reason in error.reasons
                    ]
                },
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except HouseholdCalculationUnavailableError as error:
            return _unavailable(error)
        return JSONResponse(
            accepted.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED
        )

    @router.get(
        JOB_PATH,
        summary="Get a household calculation",
        description=(
            "Return the household job as it stands: its status, the household "
            "function's result document once it has one, and its error message."
        ),
        operation_id="get_household_calculation",
        response_model=HouseholdCalculationState,
        responses={
            200: {"description": "The job as it stands"},
            403: _FORBIDDEN,
            404: {"description": "There is no such household job", **_ERROR},
            422: _UNPROCESSABLE,
            503: _UNAVAILABLE,
        },
        dependencies=[Depends(reader)],
    )
    async def get_household_calculation(household_job_id: UUID) -> JSONResponse:
        if calculations is None:
            return _not_configured()
        try:
            record = await calculations.read(household_job_id)
        except HouseholdCalculationUnavailableError as error:
            return _unavailable(error)
        if record is None:
            return _error_response(
                status.HTTP_404_NOT_FOUND,
                HouseholdCalculationError(
                    code="unknown_household_job",
                    detail=f"There is no household job {household_job_id}.",
                ),
            )
        return JSONResponse(_state(record).model_dump(mode="json"))

    app.include_router(router)
