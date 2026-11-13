"""The economy routes: create a report or a simulation, and start its calculation.

- ``POST /reports`` (permission ``simulation:submit``) creates an economy
  report, its simulations and its first run, and starts the report run's
  coordinator, answering 201 with the report's ID, its run's ID and status,
  and each side's simulation and simulation run. With ``report_type``
  ``budget_window`` it creates a budget-window report with a report run per
  year and starts up to its first five years, answering 201 with the report, the
  first year's run, the window run, its status and every year's runs. With
  ``household`` it creates a household report, a household simulation and
  run for its baseline and any reform, and its first run, and starts one
  household function call for the report run, answering 201 with the
  report, its run, its status, its year and each side's simulation and
  simulation run.
- ``POST /simulations`` (``simulation:submit``) creates a standalone
  economy simulation and its first run, and starts the run's simulation
  function, answering 201 with the simulation's ID, its run's ID and status.
- ``GET /reports/{report_id}`` and ``GET /simulations/{simulation_id}``
  (``simulation:read``) return the record with its newest run's status,
  versions, error message and times. The Public API reads results from the
  tables itself; these reads let a deploy check follow a run.

A refused request answers 400 (a row it names, or a function it needs, is
missing; ``code`` says which) or 422 (its fields, or the stored values it
names, cannot be calculated), and writes nothing. Either route answers 503
with ``Retry-After`` when it is not configured or a dependency cannot be
read, before writing anything, or when Modal refused the run's call: then
the record was created and its run failed, and the answer names it. Every
500 the routes answer is the JSON error with code ``internal_error``, and may
follow a write; a failure outside a handler's ``try`` gets the request
middleware's own plain-text 500, which the Public API reads alike.

A request gets its observability ID from the caller's header, or a new one,
and passes it to the Modal call it starts in the observability context; the
call's ``request_id`` is the request's own ``X-PolicyEngine-Request-Id``,
or a new UUID when that doesn't fit the call's context.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Final, Protocol
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from policyengine_simulation_entry.budget_window_api_models import (
    BudgetWindowReportCreated,
    BudgetWindowReportRequest,
)
from policyengine_simulation_entry.economy_api_models import (
    EconomyError,
    EconomyReportCreated,
    EconomyReportRequest,
    EconomyReportState,
    EconomyRequestUnprocessable,
    EconomySimulationCreated,
    EconomySimulationRequest,
    EconomySimulationState,
    ReportRunRead,
    SimulationRunRead,
)
from policyengine_simulation_entry.economy_submission_errors import (
    EconomySubmissionOutcomeUnknownError,
    EconomySubmissionRefusedError,
    EconomySubmissionUnavailableError,
    EconomySubmissionUnprocessableError,
)
from policyengine_simulation_entry.household_report_api_models import (
    HouseholdReportCreated,
    HouseholdReportRequest,
)
from policyengine_simulation_entry.report_api_models import ReportRequest
from policyengine_simulation_entry.report_request_errors import ReportRequestRoute
from policyengine_simulation_entry.route_support import (
    log_unexpected_failure,
    observability_context,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI
    from policyengine_simulation_persistence import (
        EconomyReportState as StoredReport,
    )
    from policyengine_simulation_persistence import (
        EconomySimulationState as StoredSimulation,
    )
    from pydantic import BaseModel

    from policyengine_simulation_entry.auth import AuthorizationDependency
    from policyengine_simulation_entry.economy_submission_errors import RecordName
    from policyengine_simulation_entry.entry_runtime import EntryRuntime

logger = logging.getLogger(__name__)

# The request IDs the observability runtime accepts, which a Modal call's
# context also carries (``Stage12InvocationContext.request_id`` takes at most
# 255 characters). Any other request ID is replaced by a new one for the call.
CALL_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._:-]{1,128}")
REPORTS_PATH = "/reports"
REPORT_PATH = "/reports/{report_id}"
SIMULATIONS_PATH = "/simulations"
SIMULATION_PATH = "/simulations/{simulation_id}"
RETRY_AFTER_SECONDS = 10

ERROR_ANSWER = {"model": EconomyError}
RETRY_AFTER_HEADER = {
    "Retry-After": {
        "description": "Seconds to wait before asking again",
        "schema": {"type": "integer"},
    }
}
FORBIDDEN_ANSWER = {
    "description": "The caller's token is missing, invalid or lacks the permission"
}
_REFUSED = {
    "description": (
        "A row the request names, or a function it needs, is missing, or the "
        "catalog disagrees with the default worker; nothing was written. "
        "unknown_region and unknown_dynamic name the caller's own region or "
        "dynamic, and unknown_dataset its region's dataset (a request names "
        "none); a household report's unknown_household and "
        "no_household_function name its household and the default worker's "
        "household function; any other code is the service's state, such as "
        "a catalog version missing while a release deploys, or, for a report "
        "on the local-area dataset, local_areas_unsupported (the default "
        "worker runs no such reports) and local_area_dataset_mismatch (the "
        "catalog's area regions use another dataset)"
    ),
    **ERROR_ANSWER,
}
_UNPROCESSABLE = {
    "description": (
        "The request's fields, or the stored policies and dynamics it names, "
        "cannot be calculated; nothing was written. Stored values that cannot "
        "be calculated carry a code: missing_parameters, "
        "overlapping_parameters, or household_not_calculable for a household "
        "report's household; no_local_area_dataset refuses data_source on a "
        "release with no local-area dataset"
    ),
    "model": EconomyRequestUnprocessable,
}
_INTERNAL_ERROR = {
    "description": (
        "The service failed unexpectedly, or could not confirm its rows were "
        "written (internal_error), possibly after creating the record"
    ),
    **ERROR_ANSWER,
}


_READ_UNPROCESSABLE = {
    "description": "The ID is not a UUID",
    "model": EconomyRequestUnprocessable,
}
_READ_UNAVAILABLE = {
    "description": (
        "Economy runs are not configured, or the database cannot be read; "
        "retry after Retry-After seconds"
    ),
    "headers": RETRY_AFTER_HEADER,
    **ERROR_ANSWER,
}
_READ_INTERNAL_ERROR = {
    "description": "The service failed unexpectedly (internal_error)",
    **ERROR_ANSWER,
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
        "headers": RETRY_AFTER_HEADER,
        **ERROR_ANSWER,
    }


class EconomySubmissions(Protocol):
    """Create reports and simulations (``ReportSubmissionService``)."""

    async def submit_report(
        self,
        request: EconomyReportRequest,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomyReportCreated:
        """Create the report and its runs and start its coordinator."""
        ...

    async def submit_budget_window(
        self,
        request: BudgetWindowReportRequest,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> BudgetWindowReportCreated:
        """Create the window's report and its years' runs, and start its first years."""
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

    async def read_report(self, report_id: UUID) -> StoredReport | None:
        """Return the report with its newest run, or None."""
        ...

    async def read_simulation(self, simulation_id: UUID) -> StoredSimulation | None:
        """Return the simulation with its newest run, or None."""
        ...


class HouseholdReportSubmissions(Protocol):
    """Create household reports (``HouseholdReportSubmissionService``)."""

    async def submit_report(
        self,
        request: HouseholdReportRequest,
        *,
        observability_context: dict[str, Any] | None,
    ) -> HouseholdReportCreated:
        """Create the report and its runs and start its household function call."""
        ...


def error_response(status_code: int, error: EconomyError) -> JSONResponse:
    """Answer ``error`` with its status, and ``Retry-After`` on a 503."""
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


def not_configured_response(
    detail: str = "Economy runs are not configured.",
) -> JSONResponse:
    """Answer 503 ``not_configured`` with ``detail``, naming what is not set up."""
    return error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        EconomyError(code="not_configured", detail=detail),
    )


# The submission service's own failures, each answered with its status.
SUBMISSION_FAILURES = (
    EconomySubmissionRefusedError,
    EconomySubmissionUnprocessableError,
    EconomySubmissionUnavailableError,
    EconomySubmissionOutcomeUnknownError,
)


def submission_failure_response(error: Exception, record: RecordName) -> JSONResponse:
    """Answer a submission the service refused or could not serve."""
    if isinstance(error, EconomySubmissionRefusedError):
        return error_response(
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
    if isinstance(error, EconomySubmissionOutcomeUnknownError):
        return _outcome_unknown_response(error)
    assert isinstance(error, EconomySubmissionUnavailableError)  # noqa: S101  # the last of SUBMISSION_FAILURES
    record_ids = {} if error.record_id is None else {f"{record}_id": error.record_id}
    return error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        EconomyError(
            code=error.code, detail=error.detail, run_id=error.run_id, **record_ids
        ),
    )


def call_request_id_for(request: Request) -> str:
    """Return the request's ID for the Modal call, or a new one that fits it.

    The request middleware keeps the caller's ``X-PolicyEngine-Request-Id``
    unchecked. A replaced ID matches none of the request's own log records;
    the observability ID, which the call's observability context carries,
    still joins them.
    """
    request_id = getattr(request.state, "request_id", "")
    if isinstance(request_id, str) and CALL_REQUEST_ID.fullmatch(request_id):
        return request_id
    return str(uuid4())


def _outcome_unknown_response(
    error: EconomySubmissionOutcomeUnknownError,
) -> JSONResponse:
    """Answer a create whose commit can't be confirmed with the JSON 500.

    The Public API reads a 500 as a record that may exist; a 503 would tell
    it nothing was created. The log is the fixed event
    ``economy_record_outcome_unknown``.
    """
    fields = {
        "record": error.record,
        "error_type": error.error_type,
        "rerun": error.rerun,
    }
    written = f"the {error.record}'s new run" if error.rerun else f"the {error.record}"
    logger.error(
        "economy_record_outcome_unknown",
        extra={**fields, "policyengine_attributes": fields},
    )
    return error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        EconomyError(
            code="internal_error",
            detail=(
                f"The service may have created {written}, but could not confirm "
                "it was written."
            ),
        ),
    )


def unexpected_failure_response(
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
    return error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        EconomyError(
            code="internal_error", detail="The service could not answer the request."
        ),
    )


def _report_state(stored: StoredReport) -> EconomyReportState:
    run = stored.run
    return EconomyReportState(
        report_id=stored.id,
        country=stored.country,
        baseline_simulation_id=stored.baseline_simulation_id,
        reform_simulation_id=stored.reform_simulation_id,
        run=None
        if run is None
        else ReportRunRead(
            run_id=run.id,
            status=run.status,
            country_package_version=run.country_package_version,
            policyengine_version=run.policyengine_version,
            error_message=run.error_message,
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            baseline_simulation_run_id=run.baseline_simulation_run_id,
            reform_simulation_run_id=run.reform_simulation_run_id,
        ),
    )


def _simulation_state(stored: StoredSimulation) -> EconomySimulationState:
    run = stored.run
    return EconomySimulationState(
        simulation_id=stored.id,
        run=None
        if run is None
        else SimulationRunRead(
            run_id=run.id,
            status=run.status,
            country_package_version=run.country_package_version,
            policyengine_version=run.policyengine_version,
            error_message=run.error_message,
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
        ),
    )


def unknown_record_response(record: RecordName, record_id: UUID) -> JSONResponse:
    """Answer 404: there is no such report or simulation."""
    return error_response(
        status.HTTP_404_NOT_FOUND,
        EconomyError(
            code="unknown_report" if record == "report" else "unknown_simulation",
            detail=f"There is no {record} {record_id}.",
        ),
    )


def add_economy_routes(  # noqa: PLR0913  # the app, its services and permissions
    app: FastAPI,
    entry: EntryRuntime,
    submissions: EconomySubmissions | None,
    *,
    submitter: AuthorizationDependency,
    reader: AuthorizationDependency,
    household_reports: HouseholdReportSubmissions | None = None,
) -> None:
    """Add the economy routes; without a service they answer 503.

    ``POST /reports`` also takes household reports, which answer 503
    without ``household_reports``.
    """
    log_string_limit = entry.runtime.config.limits.max_string_length
    app.include_router(
        _submission_routes(
            entry,
            submissions,
            household_reports,
            submitter=submitter,
            log_string_limit=log_string_limit,
        )
    )
    app.include_router(
        _read_routes(submissions, reader=reader, log_string_limit=log_string_limit)
    )


async def _economy_report_answer(
    entry: EntryRuntime,
    submissions: EconomySubmissions | None,
    body: EconomyReportRequest | BudgetWindowReportRequest,
    request: Request,
    *,
    log_string_limit: int,
) -> JSONResponse:
    """Create an economy or budget-window report; answer as ``POST /reports`` does."""
    if submissions is None:
        return not_configured_response()
    observability_id = entry.start_workflow_observability(request)
    try:
        call_request_id = call_request_id_for(request)
        context = observability_context(entry, observability_id)
        created: EconomyReportCreated | BudgetWindowReportCreated
        if isinstance(body, BudgetWindowReportRequest):
            created = await submissions.submit_budget_window(
                body, request_id=call_request_id, observability_context=context
            )
        else:
            created = await submissions.submit_report(
                body, request_id=call_request_id, observability_context=context
            )
    except SUBMISSION_FAILURES as error:
        return submission_failure_response(error, "report")
    # Every other failure is still answered as the JSON 500 declared.
    except Exception as error:  # noqa: BLE001  # answered as internal_error
        return unexpected_failure_response(
            REPORTS_PATH, error, max_characters=log_string_limit
        )
    return JSONResponse(
        created.model_dump(mode="json"), status_code=status.HTTP_201_CREATED
    )


async def _household_report_answer(
    entry: EntryRuntime,
    household_reports: HouseholdReportSubmissions | None,
    body: HouseholdReportRequest,
    request: Request,
    *,
    log_string_limit: int,
) -> JSONResponse:
    """Create a household report and answer as ``POST /reports`` does."""
    if household_reports is None:
        return not_configured_response("Household reports are not configured.")
    observability_id = entry.start_workflow_observability(request)
    try:
        created = await household_reports.submit_report(
            body, observability_context=observability_context(entry, observability_id)
        )
    except SUBMISSION_FAILURES as error:
        return submission_failure_response(error, "report")
    # Every other failure, a request the service built wrong included, is
    # still answered as the JSON 500 declared.
    except Exception as error:  # noqa: BLE001  # answered as internal_error
        return unexpected_failure_response(
            REPORTS_PATH, error, max_characters=log_string_limit
        )
    return JSONResponse(
        created.model_dump(mode="json"), status_code=status.HTTP_201_CREATED
    )


def _submission_routes(
    entry: EntryRuntime,
    submissions: EconomySubmissions | None,
    household_reports: HouseholdReportSubmissions | None,
    *,
    submitter: AuthorizationDependency,
    log_string_limit: int,
) -> APIRouter:
    """Return the router of ``POST /reports`` and ``POST /simulations``."""
    router = APIRouter()
    # A refused report body is located as an economy report's was.
    report_router = APIRouter(route_class=ReportRequestRoute)

    @report_router.post(
        REPORTS_PATH,
        summary="Create an economy, budget-window or household report",
        description=(
            "Create an economy report comparing a baseline and a reform "
            "simulation of one region's data for one year, on the v2 version "
            "manifest's default worker, and start its first run. A side whose "
            "simulation already succeeded on the same versions reuses that run. "
            "A budget-window report (report_type budget_window) compares them "
            "in each year of a window of 1 to 75 years, with a report run per "
            "year, at most five running at once; each year reuses its own "
            "year's runs. "
            "A household report (report_type household) calculates one stored "
            "household for its default year under a baseline and, optionally, "
            "a reform, each a new household simulation run, all in one "
            "household function call."
        ),
        operation_id="create_report",
        status_code=status.HTTP_201_CREATED,
        response_model=(
            EconomyReportCreated | BudgetWindowReportCreated | HouseholdReportCreated
        ),
        responses={
            201: {"description": "The report and its first runs were created"},
            400: _REFUSED,
            403: FORBIDDEN_ANSWER,
            422: _UNPROCESSABLE,
            500: _INTERNAL_ERROR,
            503: _unavailable_description("report"),
        },
        dependencies=[Depends(submitter)],
    )
    async def create_report(body: ReportRequest, request: Request) -> JSONResponse:
        if isinstance(body, HouseholdReportRequest):
            return await _household_report_answer(
                entry,
                household_reports,
                body,
                request,
                log_string_limit=log_string_limit,
            )
        return await _economy_report_answer(
            entry, submissions, body, request, log_string_limit=log_string_limit
        )

    router.include_router(report_router)

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
            403: FORBIDDEN_ANSWER,
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
            return not_configured_response()
        observability_id = entry.start_workflow_observability(request)
        try:
            created = await submissions.submit_simulation(
                body,
                request_id=call_request_id_for(request),
                observability_context=observability_context(entry, observability_id),
            )
        except SUBMISSION_FAILURES as error:
            return submission_failure_response(error, "simulation")
        # Every other failure is still answered as the JSON 500 declared.
        except Exception as error:  # noqa: BLE001  # answered as internal_error
            return unexpected_failure_response(
                SIMULATIONS_PATH, error, max_characters=log_string_limit
            )
        return JSONResponse(
            created.model_dump(mode="json"), status_code=status.HTTP_201_CREATED
        )

    return router


async def _read_answer[Stored](  # noqa: PLR0913  # the read, its record and how to answer it
    submissions: EconomySubmissions | None,
    read: Callable[[EconomySubmissions], Awaitable[Stored | None]],
    answer: Callable[[Stored], BaseModel],
    *,
    record: RecordName,
    record_id: UUID,
    log_string_limit: int,
) -> JSONResponse:
    """Answer a read: the record as it stands, 404, 503 or the JSON 500."""
    if submissions is None:
        return not_configured_response()
    route = REPORT_PATH if record == "report" else SIMULATION_PATH
    try:
        stored = await read(submissions)
        state = None if stored is None else answer(stored)
    except EconomySubmissionUnavailableError as error:
        return submission_failure_response(error, record)
    # Every other failure is still answered as the JSON 500 declared.
    except Exception as error:  # noqa: BLE001  # answered as internal_error
        return unexpected_failure_response(
            route, error, max_characters=log_string_limit
        )
    if state is None:
        return unknown_record_response(record, record_id)
    return JSONResponse(state.model_dump(mode="json"))


def _read_routes(
    submissions: EconomySubmissions | None,
    *,
    reader: AuthorizationDependency,
    log_string_limit: int,
) -> APIRouter:
    """Return the router of the report and simulation reads."""
    router = APIRouter()

    @router.get(
        REPORT_PATH,
        summary="Get a report's newest run",
        description=(
            "Return the report, its two simulations and its newest run's "
            "status, versions, error message and times."
        ),
        operation_id="get_report",
        response_model=EconomyReportState,
        responses={
            200: {"description": "The report as it stands"},
            403: FORBIDDEN_ANSWER,
            404: {
                "description": "There is no such report (unknown_report)",
                **ERROR_ANSWER,
            },
            422: _READ_UNPROCESSABLE,
            500: _READ_INTERNAL_ERROR,
            503: _READ_UNAVAILABLE,
        },
        dependencies=[Depends(reader)],
    )
    async def get_report(report_id: UUID) -> JSONResponse:
        return await _read_answer(
            submissions,
            lambda service: service.read_report(report_id),
            _report_state,
            record="report",
            record_id=report_id,
            log_string_limit=log_string_limit,
        )

    @router.get(
        SIMULATION_PATH,
        summary="Get a simulation's newest run",
        description=(
            "Return the simulation and its newest run's status, versions, error "
            "message and times."
        ),
        operation_id="get_simulation",
        response_model=EconomySimulationState,
        responses={
            200: {"description": "The simulation as it stands"},
            403: FORBIDDEN_ANSWER,
            404: {
                "description": "There is no such simulation (unknown_simulation)",
                **ERROR_ANSWER,
            },
            422: _READ_UNPROCESSABLE,
            500: _READ_INTERNAL_ERROR,
            503: _READ_UNAVAILABLE,
        },
        dependencies=[Depends(reader)],
    )
    async def get_simulation(simulation_id: UUID) -> JSONResponse:
        return await _read_answer(
            submissions,
            lambda service: service.read_simulation(simulation_id),
            _simulation_state,
            record="simulation",
            record_id=simulation_id,
            log_string_limit=log_string_limit,
        )

    return router
