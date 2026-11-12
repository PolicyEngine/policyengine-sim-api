"""The economy routes: create a report or a simulation, and start its calculation.

- ``POST /reports`` (permission ``simulation:submit``) creates an economy
  report, its simulations and its first run, and starts the report run's
  coordinator, answering 201 with the report's ID, its run's ID and status,
  and each side's simulation and simulation run.
- ``POST /simulations`` (``simulation:submit``) creates a standalone
  economy simulation and its first run, and starts the run's simulation
  function, answering 201 with the simulation's ID, its run's ID and status.

A refused request answers 400 (a row it names, or a function it needs, is
missing; ``code`` says which) or 422 (its fields, or the stored values it
names, cannot be calculated), and writes nothing. Either route answers 503
with ``Retry-After`` when it is not configured or a dependency cannot be
read, before writing anything, or when Modal refused the run's call: then
the record was created and its run failed, and the answer names it. Every
500 is the JSON error with code ``internal_error``, and may follow a write.

A request gets its observability ID from the caller's header, or a new one,
and passes it to the Modal call it starts.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from policyengine_simulation_entry.economy_api_models import (
    EconomyError,
    EconomyReportCreated,
    EconomyReportRequest,
    EconomyRequestUnprocessable,
    EconomySimulationCreated,
    EconomySimulationRequest,
)
from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionRefusedError,
    EconomySubmissionUnavailableError,
    EconomySubmissionUnprocessableError,
)
from policyengine_simulation_entry.route_support import (
    log_unexpected_failure,
    observability_context,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from policyengine_simulation_entry.auth import AuthorizationDependency
    from policyengine_simulation_entry.economy_submission_errors import RecordName
    from policyengine_simulation_entry.entry_runtime import EntryRuntime

logger = logging.getLogger(__name__)

REPORTS_PATH = "/reports"
SIMULATIONS_PATH = "/simulations"
RETRY_AFTER_SECONDS = 10

_ERROR = {"model": EconomyError}
_RETRY_AFTER = {
    "Retry-After": {
        "description": "Seconds to wait before asking again",
        "schema": {"type": "integer"},
    }
}
_FORBIDDEN = {
    "description": "The caller's token is missing, invalid or lacks the permission"
}
_REFUSED = {
    "description": (
        "A row the request names, or a function it needs, is missing; nothing "
        "was written. unknown_region, unknown_dataset and unknown_dynamic name "
        "the caller's own region, dataset or dynamic; any other code is the "
        "service's state, such as a catalog version missing while a release "
        "deploys"
    ),
    **_ERROR,
}
_UNPROCESSABLE = {
    "description": (
        "The request's fields, or the stored policies and dynamics it names, "
        "cannot be calculated; nothing was written. Stored values that cannot "
        "be calculated carry a code: missing_parameters or "
        "overlapping_parameters"
    ),
    "model": EconomyRequestUnprocessable,
}
_INTERNAL_ERROR = {
    "description": (
        "The service failed unexpectedly (internal_error), possibly after "
        "creating the record"
    ),
    **_ERROR,
}


def _unavailable_description(record: RecordName) -> dict[str, Any]:
    return {
        "description": (
            "Economy runs are not configured, or the v2 version manifest or the "
            "database cannot be read, and nothing was written; or Modal refused "
            "the run's call, or didn't accept it in time "
            f"(modal_call_refused): then the {record} named by {record}_id was "
            "created and its run failed. Retry after Retry-After seconds"
        ),
        "headers": _RETRY_AFTER,
        **_ERROR,
    }


class EconomySubmissions(Protocol):
    """Create economy reports and simulations (``EconomySubmissionService``)."""

    async def submit_report(
        self,
        request: EconomyReportRequest,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomyReportCreated:
        """Create the report and its runs and start its coordinator."""
        ...

    async def submit_simulation(
        self,
        request: EconomySimulationRequest,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomySimulationCreated:
        """Create the simulation and its run and start the run's function."""
        ...


def _error_response(status_code: int, error: EconomyError) -> JSONResponse:
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
        EconomyError(code="not_configured", detail="Economy runs are not configured."),
    )


_SUBMISSION_FAILURES = (
    EconomySubmissionRefusedError,
    EconomySubmissionUnprocessableError,
    EconomySubmissionUnavailableError,
)


def _submission_failure_response(error: Exception, record: RecordName) -> JSONResponse:
    """Answer a submission the service refused or could not serve."""
    if isinstance(error, EconomySubmissionRefusedError):
        return _error_response(
            status.HTTP_400_BAD_REQUEST,
            EconomyError(code=error.code, detail=error.detail),
        )
    if isinstance(error, EconomySubmissionUnprocessableError):
        return JSONResponse(
            {
                "detail": [
                    {"loc": list(reason.loc), "msg": reason.msg, "type": reason.type}
                    for reason in error.reasons
                ],
                "code": error.code,
            },
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    assert isinstance(error, EconomySubmissionUnavailableError)  # noqa: S101  # the last of _SUBMISSION_FAILURES
    record_ids = {} if error.record_id is None else {f"{record}_id": error.record_id}
    return _error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        EconomyError(code=error.code, detail=error.detail, **record_ids),
    )


def _unexpected_failure_response(
    route: str, error: Exception, *, max_characters: int
) -> JSONResponse:
    """Answer an unexpected failure with the JSON 500 the routes declare.

    The log is the fixed event ``economy_route_failed``; see
    ``log_unexpected_failure``.
    """
    log_unexpected_failure(
        logger,
        "economy_route_failed",
        route=route,
        error=error,
        max_characters=max_characters,
    )
    return _error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        EconomyError(
            code="internal_error", detail="The service could not answer the request."
        ),
    )


def add_economy_routes(
    app: FastAPI,
    entry: EntryRuntime,
    submissions: EconomySubmissions | None,
    *,
    submitter: AuthorizationDependency,
) -> None:
    """Add the economy routes; without a service they answer 503."""
    router = APIRouter()
    log_string_limit = entry.runtime.config.limits.max_string_length

    @router.post(
        REPORTS_PATH,
        summary="Create an economy report",
        description=(
            "Create an economy report comparing a baseline and a reform "
            "simulation of one region's data for one year, on the v2 version "
            "manifest's default worker, and start its first run. A side whose "
            "simulation already succeeded on the same versions reuses that run."
        ),
        operation_id="create_report",
        status_code=status.HTTP_201_CREATED,
        response_model=EconomyReportCreated,
        responses={
            201: {"description": "The report and its first run were created"},
            400: _REFUSED,
            403: _FORBIDDEN,
            422: _UNPROCESSABLE,
            500: _INTERNAL_ERROR,
            503: _unavailable_description("report"),
        },
        dependencies=[Depends(submitter)],
    )
    async def create_report(
        body: EconomyReportRequest, request: Request
    ) -> JSONResponse:
        if submissions is None:
            return _not_configured()
        observability_id = entry.start_workflow_observability(request)
        try:
            created = await submissions.submit_report(
                body,
                request_id=observability_id,
                observability_context=observability_context(entry, observability_id),
            )
        except _SUBMISSION_FAILURES as error:
            return _submission_failure_response(error, "report")
        # Every other failure is still answered as the JSON 500 declared.
        except Exception as error:  # noqa: BLE001  # answered as internal_error
            return _unexpected_failure_response(
                REPORTS_PATH, error, max_characters=log_string_limit
            )
        return JSONResponse(
            created.model_dump(mode="json"), status_code=status.HTTP_201_CREATED
        )

    @router.post(
        SIMULATIONS_PATH,
        summary="Create an economy simulation",
        description=(
            "Create a standalone economy simulation of one region's data for "
            "one year under one stored policy and dynamic, on the v2 version "
            "manifest's default worker, and start its first run."
        ),
        operation_id="create_simulation",
        status_code=status.HTTP_201_CREATED,
        response_model=EconomySimulationCreated,
        responses={
            201: {"description": "The simulation and its first run were created"},
            400: _REFUSED,
            403: _FORBIDDEN,
            422: _UNPROCESSABLE,
            500: _INTERNAL_ERROR,
            503: _unavailable_description("simulation"),
        },
        dependencies=[Depends(submitter)],
    )
    async def create_simulation(
        body: EconomySimulationRequest, request: Request
    ) -> JSONResponse:
        if submissions is None:
            return _not_configured()
        observability_id = entry.start_workflow_observability(request)
        try:
            created = await submissions.submit_simulation(
                body,
                request_id=observability_id,
                observability_context=observability_context(entry, observability_id),
            )
        except _SUBMISSION_FAILURES as error:
            return _submission_failure_response(error, "simulation")
        # Every other failure is still answered as the JSON 500 declared.
        except Exception as error:  # noqa: BLE001  # answered as internal_error
            return _unexpected_failure_response(
                SIMULATIONS_PATH, error, max_characters=log_string_limit
            )
        return JSONResponse(
            created.model_dump(mode="json"), status_code=status.HTTP_201_CREATED
        )

    app.include_router(router)
