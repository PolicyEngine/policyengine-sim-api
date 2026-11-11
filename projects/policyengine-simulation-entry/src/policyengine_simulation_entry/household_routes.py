"""The household calculation routes: submit a calculation, then read its job.

- ``POST /household-calculations`` (permission ``simulation:submit``) creates
  a household job and starts its household function's call, answering 202
  with the job's ID and status. A 202 ``pending`` may also be a job whose
  call Modal didn't confirm and whose state couldn't then be recorded or
  read: poll the job rather than submitting again.
- ``GET /household-calculations/{household_job_id}`` (``simulation:read``)
  returns the job as it stands: its status, the household function's result
  document once it has one, and its error message.

A refused submission answers 400 (a row it names, or the household function
it needs, is missing) or 422 (its fields, or the stored data it names, cannot
be calculated), naming why. Either route answers 503 with ``Retry-After``
when household calculations are not configured or a dependency cannot be
read, and a submission does too when Modal refuses the call and its job was
failed. Every 500 of either route, an unexpected failure or a request the
service built and the household contract refuses, is the JSON error with
code ``internal_error``.

A submission gets its observability ID from the caller's header, or a new
one, and passes it to the household function. ``household_jobs`` keeps no
observability ID, so a read returns none.
"""

from __future__ import annotations

import contextlib
import logging
import traceback
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
    HouseholdCalculationInternalError,
    HouseholdCalculationRefusedError,
    HouseholdCalculationUnavailableError,
    HouseholdCalculationUnprocessableError,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from policyengine_simulation_persistence import HouseholdJobRecord

    from policyengine_simulation_entry.auth import AuthorizationDependency
    from policyengine_simulation_entry.entry_runtime import EntryRuntime

logger = logging.getLogger(__name__)

SUBMISSION_PATH = "/household-calculations"
JOB_PATH = "/household-calculations/{household_job_id}"
RETRY_AFTER_SECONDS = 10

_ERROR = {"model": HouseholdCalculationError}
_RETRY_AFTER = {
    "Retry-After": {
        "description": "Seconds to wait before asking again",
        "schema": {"type": "integer"},
    }
}
_SUBMISSION_UNPROCESSABLE = {
    "description": (
        "The request's fields, or the stored household or policy it names, "
        "cannot be calculated: one reason per field, its location starting "
        "with the request field it traces to"
    ),
    "model": HouseholdRequestUnprocessable,
}
_SUBMISSION_UNAVAILABLE = {
    "description": (
        "Household calculations are not configured; the v2 version manifest "
        "or the database cannot be read; or Modal refused the household "
        "function's call, or didn't accept it in time, and the job named by "
        "household_job_id was failed (modal_call_refused). Retry after "
        "Retry-After seconds"
    ),
    "headers": _RETRY_AFTER,
    **_ERROR,
}
_SUBMISSION_INTERNAL_ERROR = {
    "description": (
        "The service failed unexpectedly, or built a household function "
        "request the household contract refuses (internal_error)"
    ),
    **_ERROR,
}
_READ_INTERNAL_ERROR = {
    "description": "The service failed unexpectedly (internal_error)",
    **_ERROR,
}
_READ_UNPROCESSABLE = {
    "description": "The household job ID is not a UUID",
    "model": HouseholdRequestUnprocessable,
}
_READ_UNAVAILABLE = {
    "description": (
        "Household calculations are not configured, or the database cannot "
        "be read; retry after Retry-After seconds"
    ),
    "headers": _RETRY_AFTER,
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
        """Create the household job and start its call; return it as it stands.

        A ``pending`` job may be one whose call Modal didn't confirm.
        """
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


_SUBMISSION_FAILURES = (
    HouseholdCalculationRefusedError,
    HouseholdCalculationUnprocessableError,
    HouseholdCalculationInternalError,
    HouseholdCalculationUnavailableError,
)


def _submission_failure_response(error: Exception) -> JSONResponse:
    """Answer a submission the service refused or could not serve."""
    if isinstance(error, HouseholdCalculationRefusedError):
        return _error_response(
            status.HTTP_400_BAD_REQUEST,
            HouseholdCalculationError(code=error.code, detail=error.detail),
        )
    if isinstance(error, HouseholdCalculationUnprocessableError):
        return JSONResponse(
            {
                "detail": [
                    {"loc": list(reason.loc), "msg": reason.msg, "type": reason.type}
                    for reason in error.reasons
                ]
            },
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if isinstance(error, HouseholdCalculationUnavailableError):
        return _unavailable(error)
    # The message names the refused fields the service built, never a value.
    logger.error("household_request_built_wrong: %s", error)
    return _error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        HouseholdCalculationError(
            code="internal_error",
            detail="The service could not build the household function's request.",
        ),
    )


# Separates the frames in the 500 log's ``stack_frames`` attribute.
STACK_FRAME_SEPARATOR = "; "


def _stack_frames(error: Exception, max_characters: int) -> tuple[str, int]:
    """Return the error's frames that fit ``max_characters``, and how many don't.

    Each frame is ``file:line in function``, innermost first, from where the
    error was raised out to the route's handler. The outer frames that would
    pass ``max_characters`` are left out, so the observability runtime, which
    cuts a longer string attribute at its end, keeps the innermost ones.
    """
    frames = [
        f"{frame.filename}:{frame.lineno} in {frame.name}"
        for frame in reversed(traceback.extract_tb(error.__traceback__))
    ]
    kept: list[str] = []
    length = 0
    for frame in frames:
        added = len(frame) + (len(STACK_FRAME_SEPARATOR) if kept else 0)
        if length + added > max_characters:
            break
        kept.append(frame)
        length += added
    return STACK_FRAME_SEPARATOR.join(kept), len(frames) - len(kept)


def _unexpected_failure_response(
    route: str, error: Exception, *, max_characters: int
) -> JSONResponse:
    """Answer an unexpected failure with the JSON 500 the routes declare.

    The log is the fixed event ``household_route_failed`` with the route, the
    error's type, its stack frames (file, line and function; see
    ``_stack_frames``) as one string of at most ``max_characters``, the
    observability runtime's string limit, and the number of frames left out.
    It never carries the error's message or any value, which could repeat
    request or stored data. The fields go in ``policyengine_attributes`` too,
    the only extra fields the observability runtime's log handler keeps, and
    each is a string or an integer: the runtime keeps only strings, numbers
    and booleans.
    """
    stack_frames, omitted_frame_count = _stack_frames(error, max_characters)
    fields = {
        "route": route,
        "error_type": type(error).__name__,
        "stack_frames": stack_frames,
        "stack_frames_omitted": omitted_frame_count,
    }
    logger.error(
        "household_route_failed",
        extra={**fields, "policyengine_attributes": fields},
    )
    return _error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        HouseholdCalculationError(
            code="internal_error", detail="The service could not answer the request."
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
    log_string_limit = entry.runtime.config.limits.max_string_length

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
            202: {
                "description": (
                    "The job was created and its call started; or Modal didn't "
                    "confirm the call and the job's state couldn't be recorded "
                    "or read, and it is answered pending: poll it rather than "
                    "submitting again"
                )
            },
            400: {
                "description": (
                    "A row the request names, or the household function it "
                    "needs, is missing; code says which"
                ),
                **_ERROR,
            },
            403: _FORBIDDEN,
            422: _SUBMISSION_UNPROCESSABLE,
            500: _SUBMISSION_INTERNAL_ERROR,
            503: _SUBMISSION_UNAVAILABLE,
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
        except _SUBMISSION_FAILURES as error:
            return _submission_failure_response(error)
        # Every other failure is still answered as the JSON 500 declared.
        except Exception as error:  # noqa: BLE001  # answered as internal_error
            return _unexpected_failure_response(
                SUBMISSION_PATH, error, max_characters=log_string_limit
            )
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
            422: _READ_UNPROCESSABLE,
            500: _READ_INTERNAL_ERROR,
            503: _READ_UNAVAILABLE,
        },
        dependencies=[Depends(reader)],
    )
    async def get_household_calculation(household_job_id: UUID) -> JSONResponse:
        if calculations is None:
            return _not_configured()
        try:
            record = await calculations.read(household_job_id)
            state = None if record is None else _state(record).model_dump(mode="json")
        except HouseholdCalculationUnavailableError as error:
            return _unavailable(error)
        # Every other failure is still answered as the JSON 500 declared.
        except Exception as error:  # noqa: BLE001  # answered as internal_error
            return _unexpected_failure_response(
                JOB_PATH, error, max_characters=log_string_limit
            )
        if state is None:
            return _error_response(
                status.HTTP_404_NOT_FOUND,
                HouseholdCalculationError(
                    code="unknown_household_job",
                    detail=f"There is no household job {household_job_id}.",
                ),
            )
        return JSONResponse(state)

    app.include_router(router)
