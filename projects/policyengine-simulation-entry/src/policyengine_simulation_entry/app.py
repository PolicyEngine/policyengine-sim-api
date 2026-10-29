"""FastAPI application for the Cloud Run Simulation Entrypoint.

The backend calls and comparison dispatch the routes use live in
entry_runtime; this module re-exports its public names.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from policyengine_observability import REQUEST_ID_HEADER
from policyengine_simulation_contract.gateway_models import (
    BudgetWindowBatchRequest,
    BudgetWindowBatchStatusResponse,
    BudgetWindowBatchSubmitResponse,
    HealthResponse,
    JobStatusResponse,
    JobSubmitResponse,
    PingRequest,
    PingResponse,
    SimulationErrorResponse,
    SimulationRequest,
    VersionMap,
    VersionsResponse,
)
from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ComparisonRunLifecycleStatus,
)
from policyengine_simulation_observability.identifiers import (
    OBSERVABILITY_ID_HEADER,
    normalize_observability_id,
)
from policyengine_simulation_observability.observability import (
    init_simulation_observability,
)
from policyengine_simulation_persistence import HEAD_REVISION
from starlette.datastructures import MutableHeaders

from policyengine_simulation_entry.auth import (
    AuthenticationDependency,
    AuthorizationDependency,
    CallerAuthenticator,
    CallerAuthorizer,
)
from policyengine_simulation_entry.backend import OldGatewayBackend, SimulationBackend
from policyengine_simulation_entry.budget_window_reports import YearStartingRecorder
from policyengine_simulation_entry.budget_window_routes import (
    BudgetWindowRouteServices,
    register_budget_window_routes,
)
from policyengine_simulation_entry.call_report_route import (
    CallReportRecorder,
    ModalCallerVerifier,
    register_call_report_route,
)
from policyengine_simulation_entry.config import Settings
from policyengine_simulation_entry.economy_recorders import ComparisonStartingRecorder
from policyengine_simulation_entry.economy_routes import (
    EconomyRouteServices,
    register_economy_routes,
)
from policyengine_simulation_entry.entry_runtime import (
    BACKEND_RESPONSE_HEADER,
    STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS,
    BackendCall,
    ComparisonBackend,
    EntryRuntime,
    ResponseIdentifier,
    SimulationDatabaseConnection,
    model_json,
    request_identifiers,
    route_template,
    set_observability_header,
)
from policyengine_simulation_entry.modal_identity import ModalIdentityVerifier
from policyengine_simulation_entry.schemas import (
    SimulationDatabaseReadiness,
    SimulationEntrypointReadinessResponse,
    TemporaryStage12ReportResponse,
    TemporaryStage12SubmissionResponse,
)
from policyengine_simulation_entry.simulation_database import SimulationDatabase
from policyengine_simulation_entry.stage12_backend import (
    Stage12ComparisonBackend,
    TemporaryStage12DispatchFailed,
    TemporaryStage12UnsupportedRequestError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.middleware.base import RequestResponseEndpoint

__all__ = [
    "BACKEND_RESPONSE_HEADER",
    "STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS",
    "ComparisonBackend",
    "ResponseIdentifier",
    "RunServices",
    "SimulationDatabaseConnection",
    "app",
    "create_app",
]

logger = logging.getLogger(__name__)
SIMULATION_DATABASE_READINESS_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class RunServices:
    """The simulation database and the call-ended route's seams, for tests.

    ``create_app`` builds each one it is not given: the database from
    ``SIMULATION_DATABASE_URL``, the call recorder from that database, and the
    Modal identity verifier from the expected Modal caller.
    """

    database: SimulationDatabaseConnection | None = None
    call_recorder: CallReportRecorder | None = None
    modal_identity_verifier: ModalCallerVerifier | None = None
    economy: EconomyRouteServices | None = None
    budget_windows: BudgetWindowRouteServices | None = None


def _temporary_submission_payload(
    report: ComparisonReportRecord,
) -> TemporaryStage12SubmissionResponse:
    """Describe a submitted temporary report and where to poll it."""
    return TemporaryStage12SubmissionResponse(
        evaluation_id=report.evaluation_id,
        status=report.status,
        poll_url=f"/internal/stage12/reports/{report.evaluation_id}",
    )


def _stage12_unavailable_response(detail: str) -> JSONResponse:
    """Return the 503 the temporary Stage 12 routes send when they cannot run."""
    return JSONResponse(
        status_code=503,
        content={"detail": detail},
        headers={"Retry-After": "10"},
    )


def _install_request_context(app: FastAPI, settings: Settings) -> None:
    """Add the middleware that assigns request IDs and correlation headers."""

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        headers = MutableHeaders(scope=request.scope)
        request_id = headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        incoming_observability_id = normalize_observability_id(
            headers.get(OBSERVABILITY_ID_HEADER)
        )
        headers[REQUEST_ID_HEADER] = request_id
        request.state.request_id = request_id
        request.state.incoming_observability_id = incoming_observability_id
        request.state.observability_id = None
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "simulation_entry_unhandled_request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": route_template(request),
                    **request_identifiers(request),
                },
            )
            response = Response(
                content="Internal Server Error",
                status_code=500,
                media_type="text/plain",
                headers={REQUEST_ID_HEADER: request_id},
            )
        set_observability_header(
            response, normalize_observability_id(request.state.observability_id)
        )
        if settings.revision:
            response.headers["X-PolicyEngine-Simulation-Revision"] = settings.revision
        return response


def _register_temporary_stage12_submission(
    app: FastAPI,
    entry: EntryRuntime,
    stage12_operator: AuthorizationDependency,
) -> None:
    """Add the operator-only route that submits a temporary Stage 12 report."""

    # TEMPORARY(Stage 12): This operator-only route is deliberately excluded
    # from OpenAPI. Remove it when Stage 14 provides authoritative v2 report
    # submission and polling through production report records.
    @app.post(
        "/internal/stage12/reports",
        include_in_schema=False,
        dependencies=[Depends(stage12_operator)],
    )
    async def submit_temporary_stage12_report(
        body: SimulationRequest,
        request: Request,
    ) -> Response:
        comparison = entry.comparison
        if not entry.settings.stage12_enabled or comparison is None:
            return _stage12_unavailable_response(
                "Stage 12 direct execution is unavailable."
                if entry.settings.stage12_enabled
                else "Stage 12 execution is disabled."
            )
        observability_id = entry.start_workflow_observability(request)
        try:
            report = await asyncio.wait_for(
                comparison.submit_temporary_report(
                    request_payload=model_json(body),
                    request_id=request.state.request_id,
                    observability_id=observability_id,
                ),
                timeout=STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return JSONResponse(
                status_code=504,
                content={"detail": "Stage 12 submission acknowledgement timed out."},
                headers={"Retry-After": "10"},
            )
        except TemporaryStage12UnsupportedRequestError as error:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "Stage 12 does not support this request.",
                    "reason": error.reason,
                },
            )
        except TemporaryStage12DispatchFailed as error:
            return JSONResponse(
                status_code=502,
                content=_temporary_submission_payload(error.report).model_dump(
                    mode="json"
                ),
            )
        # This temporary diagnostic route must convert every integration
        # failure into an HTTP response rather than crash the application.
        except Exception as error:  # noqa: BLE001  # every integration failure becomes a 503
            logger.error(  # noqa: TRY400  # Stage 12 failure logs carry only the error type; a traceback could expose request or calculation data
                "stage12_direct_submission_failed",
                extra={
                    "request_id": request.state.request_id,
                    "error_type": type(error).__name__,
                },
            )
            return _stage12_unavailable_response(
                "Stage 12 direct execution is unavailable."
            )
        return JSONResponse(
            status_code=202,
            content=_temporary_submission_payload(report).model_dump(mode="json"),
            # The Modal coordinator, rather than Cloud Run, creates the durable
            # parent row. Give it a brief head start before the first status read.
            headers={"Retry-After": "1"},
        )


def _register_temporary_stage12_status(
    app: FastAPI,
    entry: EntryRuntime,
    stage12_operator: AuthorizationDependency,
) -> None:
    """Add the operator-only route that reads a temporary Stage 12 report."""

    # TEMPORARY(Stage 12): Read temporary comparison state through the
    # API-owned persistence service; never wait on or poll a Modal FunctionCall
    # from this Cloud Run request.
    @app.get(
        "/internal/stage12/reports/{evaluation_id}",
        include_in_schema=False,
        dependencies=[Depends(stage12_operator)],
    )
    async def get_temporary_stage12_report(
        evaluation_id: UUID,
        request: Request,
    ) -> Response:
        comparison = entry.comparison
        if comparison is None:
            return _stage12_unavailable_response(
                "Stage 12 direct execution is unavailable."
            )
        try:
            report, simulations = await comparison.get_temporary_report(evaluation_id)
        except LookupError:
            return JSONResponse(
                status_code=404,
                content={"detail": "Stage 12 report was not found."},
                # A newly acknowledged Modal invocation may not yet have
                # created its parent row. The same response also covers an
                # identifier that never existed, so callers must bound retries.
                headers={"Retry-After": "1"},
            )
        # Reading temporary diagnostic state crosses an internal HTTP boundary
        # whose concrete exception types are implementation details.
        except Exception as error:  # noqa: BLE001  # any status-read failure becomes a 503
            logger.error(  # noqa: TRY400  # Stage 12 failure logs carry only the error type; a traceback could expose request or calculation data
                "stage12_direct_status_failed",
                extra={
                    "request_id": request.state.request_id,
                    "evaluation_id": str(evaluation_id),
                    "error_type": type(error).__name__,
                },
            )
            return _stage12_unavailable_response(
                "Stage 12 report status is unavailable."
            )
        payload = TemporaryStage12ReportResponse(
            report=report,
            simulations=simulations,
        )
        running = report.status in {
            ComparisonRunLifecycleStatus.PENDING,
            ComparisonRunLifecycleStatus.RUNNING,
        }
        response_headers = {"Retry-After": "5"} if running else {}
        report_observability_id = entry.bind_observability_id(
            request,
            report.observability_id,
        )
        if report_observability_id is not None:
            response_headers[OBSERVABILITY_ID_HEADER] = report_observability_id
        return JSONResponse(
            status_code=202 if running else 200,
            content=payload.model_dump(mode="json"),
            headers=response_headers or None,
        )


def _register_simulation_routes(
    app: FastAPI,
    entry: EntryRuntime,
    *,
    submitter: AuthorizationDependency,
    reader: AuthorizationDependency,
) -> None:
    """Add the authenticated submission and polling routes the gateway serves."""

    @app.post(
        "/simulate/economy/comparison",
        summary="Submit Simulation",
        description=(
            "Submit a simulation job.\n\n"
            "Routes to the appropriate simulation app based on country and version.\n"
            "Returns immediately with a job_id for polling."
        ),
        operation_id="submit_simulation_simulate_economy_comparison_post",
        response_model=JobSubmitResponse,
        response_model_exclude_none=True,
        responses={
            200: {"description": "Job submitted successfully"},
            400: {
                "description": "Invalid request or SPM selection",
                "model": SimulationErrorResponse,
            },
        },
        dependencies=[Depends(submitter)],
    )
    async def submit_comparison(
        body: SimulationRequest,
        request: Request,
    ) -> Response:
        observability_id = entry.start_workflow_observability(request)
        request_payload = model_json(body)
        response = await entry.forward(
            request,
            BackendCall(
                "POST",
                "/simulate/economy/comparison",
                request_payload,
                response_identifier_key="job_id",
            ),
        )
        await entry.dispatch_comparison_run(
            request, request_payload, response, observability_id
        )
        return response

    @app.post(
        "/simulate/economy/budget-window",
        summary="Submit Budget Window Batch",
        description=(
            "Submit a budget-window batch job.\n\n"
            "Returns immediately with a parent batch job ID for polling."
        ),
        operation_id=("submit_budget_window_batch_simulate_economy_budget_window_post"),
        response_model=BudgetWindowBatchSubmitResponse,
        response_model_exclude_none=True,
        responses={
            200: {"description": "Budget-window batch submitted successfully"},
            400: {
                "description": "Invalid request or SPM selection",
                "model": SimulationErrorResponse,
            },
        },
        dependencies=[Depends(submitter)],
    )
    async def submit_budget_window(
        body: BudgetWindowBatchRequest,
        request: Request,
    ) -> Response:
        entry.start_workflow_observability(request)
        return await entry.forward(
            request,
            BackendCall(
                "POST",
                "/simulate/economy/budget-window",
                model_json(body),
                response_identifier_key="batch_job_id",
            ),
        )

    @app.get(
        "/jobs/{job_id}",
        summary="Get Job Status",
        description=(
            "Poll for job status.\n\n"
            "Returns:\n"
            '    - 200 with status="complete" and result when done\n'
            '    - 202 with status="running" while in progress\n'
            "    - 404 if job_id not found\n"
            '    - 500 with status="failed" and error on failure'
        ),
        operation_id="get_job_status_jobs__job_id__get",
        response_model=JobStatusResponse,
        response_model_exclude_none=True,
        responses={
            200: {"description": "Job complete", "model": JobStatusResponse},
            202: {"description": "Job still running"},
            400: {
                "description": "SPM input or configuration error",
                "model": JobStatusResponse,
            },
            404: {"description": "Job not found"},
            500: {"description": "Job failed"},
        },
        dependencies=[Depends(reader)],
    )
    async def get_job(job_id: str, request: Request) -> Response:
        return await entry.forward(
            request,
            BackendCall("GET", f"/jobs/{job_id}", identifiers={"job_id": job_id}),
        )

    @app.get(
        "/budget-window-jobs/{batch_job_id}",
        summary="Get Budget Window Job Status",
        description="Poll for budget-window batch status.",
        operation_id=(
            "get_budget_window_job_status_budget_window_jobs__batch_job_id__get"
        ),
        response_model=BudgetWindowBatchStatusResponse,
        response_model_exclude_none=True,
        responses={
            200: {
                "description": "Batch complete",
                "model": BudgetWindowBatchStatusResponse,
            },
            202: {"description": "Batch submitted or running"},
            400: {
                "description": "SPM input or configuration error",
                "model": BudgetWindowBatchStatusResponse,
            },
            404: {"description": "Batch job not found"},
            500: {"description": "Batch failed"},
        },
        dependencies=[Depends(reader)],
    )
    async def get_budget_window_job(
        batch_job_id: str,
        request: Request,
    ) -> Response:
        return await entry.forward(
            request,
            BackendCall(
                "GET",
                f"/budget-window-jobs/{batch_job_id}",
                identifiers={"batch_job_id": batch_job_id},
            ),
        )


async def _simulation_database_readiness(
    database: SimulationDatabaseConnection,
) -> SimulationDatabaseReadiness:
    """Report the database's applied revision next to the image's."""
    try:
        revision = await asyncio.wait_for(
            database.read_revision(),
            timeout=SIMULATION_DATABASE_READINESS_TIMEOUT_SECONDS,
        )
    # Any failure to reach or read the database is reported, never raised:
    # the old-gateway routes this check covers do not use it.
    except Exception as error:  # noqa: BLE001  # reported as unreachable
        logger.warning(
            "simulation_database_unreachable",
            extra={"error_type": type(error).__name__},
        )
        return SimulationDatabaseReadiness(
            reachable=False,
            revision=None,
            expected_revision=HEAD_REVISION,
        )
    return SimulationDatabaseReadiness(
        reachable=True,
        revision=revision,
        expected_revision=HEAD_REVISION,
    )


def _register_service_routes(app: FastAPI, entry: EntryRuntime) -> None:
    """Add the public version, health, readiness and ping routes."""

    @app.get(
        "/versions",
        summary="List Versions",
        description="List all available routing versions.",
        operation_id="list_versions_versions_get",
        response_model=VersionsResponse,
    )
    async def list_versions(request: Request) -> Response:
        return await entry.forward(request, BackendCall("GET", "/versions"))

    @app.get(
        "/versions/{kind}",
        summary="Get Country Versions",
        description="Get available versions for policyengine, US, or UK routing.",
        operation_id="get_country_versions_versions__kind__get",
        response_model=VersionMap,
    )
    async def get_country_versions(kind: str, request: Request) -> Response:
        return await entry.forward(request, BackendCall("GET", f"/versions/{kind}"))

    @app.get(
        "/health",
        description="Health check endpoint.",
        operation_id="health_health_get",
        response_model=HealthResponse,
    )
    async def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/ready", response_model=SimulationEntrypointReadinessResponse)
    async def ready() -> Response:
        backend_ready = await entry.backend.ready()
        status = "ready" if backend_ready else "not_ready"
        status_code = 200 if backend_ready else 503
        if entry.database is None:
            # Without a configured database the response keeps its old shape.
            readiness = SimulationEntrypointReadinessResponse(status=status)
            return JSONResponse(
                readiness.model_dump(mode="json", exclude={"simulation_database"}),
                status_code=status_code,
            )
        readiness = SimulationEntrypointReadinessResponse(
            status=status,
            simulation_database=await _simulation_database_readiness(entry.database),
        )
        return JSONResponse(readiness.model_dump(mode="json"), status_code=status_code)

    @app.post(
        "/ping",
        description="Verify the API is able to receive and process requests.",
        operation_id="ping_ping_post",
        response_model=PingResponse,
    )
    async def ping(body: PingRequest, request: Request) -> Response:
        return await entry.forward(
            request, BackendCall("POST", "/ping", model_json(body))
        )


def _report_paths(
    services: RunServices,
    runtime_settings: Settings,
    runtime_database: SimulationDatabaseConnection | None,
) -> tuple[
    CallReportRecorder | None,
    EconomyRouteServices | None,
    BudgetWindowRouteServices | None,
]:
    """Return the call recorder and the report paths, building each one not given.

    The economy and budget-window paths need the simulation database and the
    Modal environment calls start in. A reported call's end starts the
    comparisons and budget-window years it lets start.
    """
    call_recorder = services.call_recorder
    if call_recorder is None and isinstance(runtime_database, SimulationDatabase):
        call_recorder = runtime_database.call_recorder()
    economy = services.economy
    if (
        economy is None
        and isinstance(runtime_database, SimulationDatabase)
        and runtime_settings.call_report_modal_environment
    ):
        economy = runtime_database.economy_routes(
            artifact_environment=runtime_settings.environment,
            modal_environment=runtime_settings.call_report_modal_environment,
        )
    budget_windows = services.budget_windows
    if (
        budget_windows is None
        and isinstance(runtime_database, SimulationDatabase)
        and economy is not None
    ):
        budget_windows = runtime_database.budget_window_routes(economy)
    if call_recorder is not None and economy is not None:
        call_recorder = ComparisonStartingRecorder(call_recorder, economy.reports)
    if call_recorder is not None and budget_windows is not None:
        call_recorder = YearStartingRecorder(call_recorder, budget_windows.reports)
    return call_recorder, economy, budget_windows


def create_app(
    *,
    settings: Settings | None = None,
    backend: SimulationBackend | None = None,
    auth_dependency: AuthenticationDependency | None = None,
    comparison_backend: ComparisonBackend | None = None,
    run_services: RunServices | None = None,
) -> FastAPI:
    """Build the app with injectable auth/backend seams for hermetic tests."""
    runtime_settings = settings or Settings.from_env()
    services = run_services or RunServices()
    runtime_database = services.database
    if runtime_database is None and runtime_settings.simulation_database_url:
        runtime_database = SimulationDatabase.from_url(
            runtime_settings.simulation_database_url
        )
    call_recorder, economy, budget_windows = _report_paths(
        services, runtime_settings, runtime_database
    )
    modal_identity_verifier = services.modal_identity_verifier
    expected_modal_caller = runtime_settings.expected_modal_caller
    if modal_identity_verifier is None and expected_modal_caller is not None:
        modal_identity_verifier = ModalIdentityVerifier(expected_modal_caller)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runtime_settings.validate()
        await runtime_backend.start()
        try:
            yield
        finally:
            await runtime_backend.close()
            if runtime_database is not None:
                runtime_database.dispose()
            runtime.shutdown()

    app = FastAPI(
        title="PolicyEngine Simulation Entrypoint",
        description=("Authenticated simulation submission and polling control plane."),
        version="1.0.0",
        lifespan=lifespan,
    )

    runtime = init_simulation_observability(
        app,
        service_name="policyengine-simulation-entry",
        service_role="simulation_entry",
        platform="google_cloud_run",
        environment=runtime_settings.environment,
    )

    runtime_backend = backend or OldGatewayBackend(runtime_settings, runtime=runtime)
    authenticate = auth_dependency or CallerAuthenticator(runtime_settings, runtime)
    authorizer = CallerAuthorizer(
        authenticate=authenticate,
        enforce=runtime_settings.enforce_permissions,
        runtime=runtime,
    )
    simulation_submitter = authorizer.require_permission("simulation:submit")
    simulation_reader = authorizer.require_permission("simulation:read")
    stage12_operator = authorizer.require_permission("stage12:operate")
    runtime_comparison = comparison_backend
    if runtime_comparison is None and runtime_settings.stage12_resources_configured:
        runtime_comparison = Stage12ComparisonBackend.from_settings(
            runtime_settings,
            runtime=runtime,
        )
    entry = EntryRuntime(
        settings=runtime_settings,
        runtime=runtime,
        backend=runtime_backend,
        comparison=runtime_comparison,
        database=runtime_database,
    )

    _install_request_context(app, runtime_settings)
    _register_temporary_stage12_submission(app, entry, stage12_operator)
    _register_temporary_stage12_status(app, entry, stage12_operator)
    _register_simulation_routes(
        app,
        entry,
        submitter=simulation_submitter,
        reader=simulation_reader,
    )
    _register_service_routes(app, entry)
    register_call_report_route(
        app, verifier=modal_identity_verifier, recorder=call_recorder
    )
    register_economy_routes(
        app,
        services=economy,
        submitter=simulation_submitter,
        reader=simulation_reader,
    )
    register_budget_window_routes(
        app,
        services=budget_windows,
        submitter=simulation_submitter,
        reader=simulation_reader,
    )
    return app


app = create_app()
