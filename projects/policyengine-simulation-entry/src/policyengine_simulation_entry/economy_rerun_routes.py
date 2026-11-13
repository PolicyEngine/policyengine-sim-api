"""The re-run routes: calculate a stored report or simulation again as a new run.

- ``POST /reports/{report_id}/runs`` (permission ``simulation:submit``)
  joins the stored report's run on the v2 version manifest's default
  worker's versions, or creates its new run and starts its coordinator,
  answering 201 with the report's ID, the run's ID and status, and each
  side's simulation and simulation run. For an economy or budget-window
  report, ``{"force": true}`` calculates both simulations again, as an
  extra run marked forced that nothing joins; otherwise a side reuses a run
  of its own simulation that succeeded on the same versions.
  A budget-window report's new run is a budget-window run with a report run
  per year, up to five of them started at once as a new window's are; it
  answers as the budget-window create does, naming the window run and every
  year's runs. A household report's new run gives each side a new run of its
  own household simulation, never reusing one, started by one household
  function call; it answers as the household report create does.
- ``POST /simulations/{simulation_id}/runs`` (``simulation:submit``) joins
  the stored simulation's run on the current versions that hasn't failed,
  pending, running or succeeded, or else creates one and starts its
  simulation function, answering 201 with the simulation's ID, the run's ID
  and its status.

Every earlier run is kept. A re-run answers 404 for a record that does not
exist, and 409 (``run_in_progress``, naming the run in ``run_id``) while a
budget-window report has a window run or year run pending or running and
the re-run isn't forced, or while a household report has a run pending or
running (a household re-run ignores ``force``), or for a record this
service does not re-run (``rerun_not_supported``); then nothing is
written. Without ``force``, an economy report's re-run joins the report's
run on the current versions that hasn't failed and wasn't forced, pending,
running or succeeded, and answers it with no new call. The other
answers are the creates': 400 or 422 when the stored record cannot be
calculated on the current catalog version, 503 with ``Retry-After`` before
any write or when Modal refused the new run's call (then the run was failed,
and the answer names the record and the run), and the JSON 500.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from policyengine_simulation_entry.budget_window_api_models import (
    BudgetWindowReportCreated,
)
from policyengine_simulation_entry.economy_api_models import (
    EconomyError,
    EconomyReportCreated,
    EconomyRequestUnprocessable,
    EconomySimulationCreated,
    ReportRerunRequest,
)
from policyengine_simulation_entry.economy_routes import (
    ERROR_ANSWER,
    FORBIDDEN_ANSWER,
    RETRY_AFTER_HEADER,
    SUBMISSION_FAILURES,
    EconomySubmissions,
    call_request_id_for,
    error_response,
    not_configured_response,
    submission_failure_response,
    unexpected_failure_response,
    unknown_record_response,
)
from policyengine_simulation_entry.economy_submission_errors import (
    EconomyRecordNotFoundError,
    EconomyRerunConflictError,
)
from policyengine_simulation_entry.household_report_api_models import (
    HouseholdReportCreated,
)
from policyengine_simulation_entry.route_support import observability_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI
    from pydantic import BaseModel

    from policyengine_simulation_entry.auth import AuthorizationDependency
    from policyengine_simulation_entry.economy_submission_errors import RecordName
    from policyengine_simulation_entry.entry_runtime import EntryRuntime

REPORT_RUNS_PATH = "/reports/{report_id}/runs"
SIMULATION_RUNS_PATH = "/simulations/{simulation_id}/runs"


class EconomyReruns(Protocol):
    """Re-run stored reports and economy simulations (``EconomyRerunService``)."""

    async def rerun_report(
        self,
        report_id: UUID,
        request: ReportRerunRequest,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomyReportCreated | BudgetWindowReportCreated | HouseholdReportCreated:
        """Create the report's new run and start it, or a window's first years."""
        ...

    async def rerun_simulation(
        self,
        simulation_id: UUID,
        *,
        request_id: str,
        observability_context: dict[str, Any] | None,
    ) -> EconomySimulationCreated:
        """Create the simulation's new run and start its function."""
        ...


class EconomyService(EconomySubmissions, EconomyReruns, Protocol):
    """Create, read and re-run economy reports and simulations."""


# The run a 409 run_in_progress names, by the record re-run.
_CONFLICTS: dict[RecordName, str] = {
    "report": (
        "a budget-window report has a window run or year run pending or "
        "running and the re-run isn't forced, or a household report has a "
        "run pending or running, forced or not (run_in_progress; run_id "
        "names the report's budget-window run, or else its report run), or "
        "the service does not re-run it (rerun_not_supported)"
    ),
    "simulation": "the service does not re-run it (rerun_not_supported)",
}
# The 400 codes that name a stored record's own rows on its re-run: a
# report's re-run also names its stored simulations (missing_reference) and
# a household report's its household.
_STORED_ROW_CODES: dict[RecordName, str] = {
    "report": (
        "unknown_region, unknown_dataset, unknown_dynamic, unknown_policy, "
        "unknown_household and missing_reference"
    ),
    "simulation": "unknown_region, unknown_dataset, unknown_dynamic and unknown_policy",
}
# What only a household report's re-run answers, by status, after the
# economy codes.
_HOUSEHOLD_ANSWERS: dict[RecordName, dict[int, str]] = {
    "report": {
        400: "; or a household report's household no longer exists",
        422: (
            "; or the stored household cannot be calculated "
            "(household_not_calculable, located under report)"
        ),
        503: "Economy runs or household reports are not configured",
    },
    "simulation": {503: "Economy runs are not configured"},
}


def _responses(record: RecordName) -> dict[int | str, dict[str, Any]]:
    """Return every answer a re-run of ``record`` declares."""
    return {
        201: {
            "description": (
                f"The {record}'s run the re-run ended on: one it joined, pending, "
                "running or succeeded on the default worker's versions, which "
                "gets no new call, or one it created and started"
            )
        },
        400: {
            "description": (
                f"The stored {record}'s region, dataset, policy or dynamic is "
                "missing from the catalog version of the default worker's .py "
                f"version, or a function the {record} needs is missing"
                f"{_HOUSEHOLD_ANSWERS[record].get(400, '')}; nothing was written. "
                f"{_STORED_ROW_CODES[record]} name the stored {record}'s own "
                "rows; any other code is the service's state"
            ),
            **ERROR_ANSWER,
        },
        403: FORBIDDEN_ANSWER,
        404: {
            "description": f"There is no such {record} (unknown_{record})",
            **ERROR_ANSWER,
        },
        409: {
            "description": (
                f"The {record} is not re-run now, and nothing was written: "
                f"{_CONFLICTS[record]}"
            ),
            **ERROR_ANSWER,
        },
        422: {
            "description": (
                "The ID or the body is malformed, or the stored policies and "
                "dynamics cannot be calculated on the current catalog version "
                "(missing_parameters or overlapping_parameters, located under "
                f"{record}){_HOUSEHOLD_ANSWERS[record].get(422, '')}; nothing was "
                "written"
            ),
            "model": EconomyRequestUnprocessable,
        },
        500: {
            "description": (
                "The service failed unexpectedly, or could not confirm the new "
                "run was written (internal_error), possibly after creating it"
            ),
            **ERROR_ANSWER,
        },
        503: {
            "description": (
                f"{_HOUSEHOLD_ANSWERS[record][503]}, or the v2 version manifest or "
                "the database cannot be read, and nothing was written; or Modal "
                "refused the new run's call, or didn't accept it in time "
                f"(modal_call_refused): then the {record} named by {record}_id "
                "has a new run, named by run_id, which was failed. Retry after "
                "Retry-After seconds"
            ),
            "headers": RETRY_AFTER_HEADER,
            **ERROR_ANSWER,
        },
    }


def _rerun_failure_response(
    error: Exception, record: RecordName, record_id: UUID
) -> JSONResponse:
    """Answer a re-run the service refused or could not serve."""
    if isinstance(error, EconomyRecordNotFoundError):
        return unknown_record_response(record, record_id)
    if isinstance(error, EconomyRerunConflictError):
        return error_response(
            status.HTTP_409_CONFLICT,
            EconomyError(code=error.code, detail=error.detail, run_id=error.run_id),
        )
    return submission_failure_response(error, record)


_RERUN_FAILURES = (
    *SUBMISSION_FAILURES,
    EconomyRecordNotFoundError,
    EconomyRerunConflictError,
)


def add_economy_rerun_routes(
    app: FastAPI,
    entry: EntryRuntime,
    reruns: EconomyReruns | None,
    *,
    submitter: AuthorizationDependency,
) -> None:
    """Add the re-run routes; without a service they answer 503."""
    log_string_limit = entry.runtime.config.limits.max_string_length
    router = APIRouter()

    async def answer(  # the route, its record and how to re-run it
        request: Request,
        rerun: Callable[
            [EconomyReruns, str, dict[str, Any] | None], Awaitable[BaseModel]
        ],
        *,
        route: str,
        record: RecordName,
        record_id: UUID,
    ) -> JSONResponse:
        if reruns is None:
            return not_configured_response()
        observability_id = entry.start_workflow_observability(request)
        try:
            created = await rerun(
                reruns,
                call_request_id_for(request),
                observability_context(entry, observability_id),
            )
        except _RERUN_FAILURES as error:
            return _rerun_failure_response(error, record, record_id)
        # Every other failure is still answered as the JSON 500 declared.
        except Exception as error:  # noqa: BLE001  # answered as internal_error
            return unexpected_failure_response(
                route, error, max_characters=log_string_limit
            )
        return JSONResponse(
            created.model_dump(mode="json"), status_code=status.HTTP_201_CREATED
        )

    @router.post(
        REPORT_RUNS_PATH,
        summary="Re-run an economy, budget-window or household report",
        description=(
            "Create the stored report's new run on the v2 version manifest's "
            "default worker, and start it; every earlier run is kept. Without "
            "force, an economy report's re-run joins the report's run on those "
            "versions that hasn't failed and wasn't forced, pending, running "
            "or succeeded, and answers it with no new call; otherwise a side "
            "reuses a run of its own simulation that succeeded on the same "
            "versions. With force, an economy or budget-window report's "
            "simulations are calculated again, as an extra run marked forced "
            "that nothing joins; a household report's re-run ignores force. "
            "A budget-window report's new run is a "
            "budget-window run with a report run per year, up to five of them "
            "started at once, each year on its own year's simulations. A "
            "household report's new run calculates its stored household in "
            "the report's year with a new run of each side's own household "
            "simulation, never reused, all in one household function call."
        ),
        operation_id="rerun_report",
        status_code=status.HTTP_201_CREATED,
        response_model=(
            EconomyReportCreated | BudgetWindowReportCreated | HouseholdReportCreated
        ),
        responses=_responses("report"),
        dependencies=[Depends(submitter)],
    )
    async def rerun_report(
        report_id: UUID, body: ReportRerunRequest, request: Request
    ) -> JSONResponse:
        return await answer(
            request,
            lambda service, request_id, context: service.rerun_report(
                report_id,
                body,
                request_id=request_id,
                observability_context=context,
            ),
            route=REPORT_RUNS_PATH,
            record="report",
            record_id=report_id,
        )

    @router.post(
        SIMULATION_RUNS_PATH,
        summary="Re-run an economy simulation",
        description=(
            "Join the stored simulation's run on the v2 version manifest's "
            "default worker's versions that hasn't failed, pending, running or "
            "succeeded, or else create one and start it; every earlier run is "
            "kept."
        ),
        operation_id="rerun_simulation",
        status_code=status.HTTP_201_CREATED,
        response_model=EconomySimulationCreated,
        responses=_responses("simulation"),
        dependencies=[Depends(submitter)],
    )
    async def rerun_simulation(simulation_id: UUID, request: Request) -> JSONResponse:
        return await answer(
            request,
            lambda service, request_id, context: service.rerun_simulation(
                simulation_id, request_id=request_id, observability_context=context
            ),
            route=SIMULATION_RUNS_PATH,
            record="simulation",
            record_id=simulation_id,
        )

    app.include_router(router)
