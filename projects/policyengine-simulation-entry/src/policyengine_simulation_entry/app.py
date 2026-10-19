"""FastAPI application for the Cloud Run Simulation Entrypoint."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
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
from policyengine_simulation_contract.json_types import JsonObject
from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
)
from policyengine_simulation_observability.identifiers import (
    OBSERVABILITY_ID_HEADER,
    normalize_observability_id,
    resolve_observability_id,
)
from policyengine_simulation_observability.observability import (
    init_simulation_observability,
)
from policyengine_simulation_persistence import HEAD_REVISION
from policyengine_simulation_persistence.jobs import JobStore
from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.datastructures import MutableHeaders

from policyengine_simulation_entry.auth import (
    AuthenticationDependency,
    CallerAuthenticator,
    CallerAuthorizer,
)
from policyengine_simulation_entry.backend import (
    BackendResponse,
    BackendTimeoutError,
    BackendUnavailableError,
    OldGatewayBackend,
    SimulationBackend,
)
from policyengine_simulation_entry.config import Settings
from policyengine_simulation_entry.google_oidc import (
    GoogleOidcVerifier,
    InternalCallerRejectedError,
)
from policyengine_simulation_entry.schemas import (
    BackendTelemetryAttributes,
    BackendTelemetryPayload,
    RequestIdentifiers,
    SimulationDatabaseReadiness,
    SimulationEntrypointReadinessResponse,
    SimulationJobCollectionResponse,
    SimulationJobError,
    SimulationJobStatusResponse,
    SimulationJobSweepResponse,
    TemporaryStage12ReportResponse,
    TemporaryStage12SubmissionResponse,
)
from policyengine_simulation_entry.simulation_database import SimulationDatabase
from policyengine_simulation_entry.simulation_jobs import (
    REGISTERED_JOB_KINDS,
    CloudTasksCollectionQueue,
    JobView,
    ModalCallDispatcher,
    ModalCallInspector,
    SimulationJobService,
)
from policyengine_simulation_entry.stage12_backend import (
    Stage12ComparisonBackend,
    TemporaryStage12DispatchFailed,
    TemporaryStage12UnsupportedRequestError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.middleware.base import RequestResponseEndpoint

logger = logging.getLogger(__name__)
BACKEND_RESPONSE_HEADER = {
    "X-PolicyEngine-Simulation-Backend": "old_gateway",
}
_json_object_adapter = TypeAdapter(JsonObject)
type ResponseIdentifier = Literal["job_id", "batch_job_id"]
STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS = 5.0
SIMULATION_DATABASE_READINESS_TIMEOUT_SECONDS = 3.0
SIMULATION_JOB_RETRY_AFTER_SECONDS = "5"
_internal_bearer = HTTPBearer(auto_error=False)


class InternalCallerVerifier(Protocol):
    """Verify the token on a Cloud Tasks or Cloud Scheduler request.

    The real implementation is ``GoogleOidcVerifier``; tests inject fakes.
    """

    def verify(self, token: str) -> None:
        """Return when the token is valid; raise InternalCallerRejectedError."""
        ...


class SimulationDatabaseConnection(Protocol):
    """The Simulation Entrypoint's own database, as the routes use it.

    The real implementation is ``SimulationDatabase``; tests inject fakes.
    """

    async def read_revision(self) -> str | None:
        """Return the applied migration revision, or None before the first one."""
        ...

    def dispose(self) -> None:
        """Close every pooled connection."""
        ...


class ComparisonBackend(Protocol):
    """Stage 12 comparison dispatch and polling seam used by the entry routes.

    The real implementation is ``Stage12ComparisonBackend``; tests inject fakes.
    The routes bound every call with a timeout and never let a failure here
    alter the production v1 response.
    """

    async def dispatch_after_production(
        self,
        *,
        request_payload: dict[str, Any],
        production_response: bytes,
        request_id: str,
        observability_id: str,
    ) -> None:
        """Start a shadow comparison run for a production job the gateway accepted.

        ``production_response`` is the raw gateway response body; its ``job_id``
        identifies the incumbent run being compared.
        """
        ...

    async def submit_temporary_report(
        self,
        *,
        request_payload: dict[str, Any],
        request_id: str,
        observability_id: str,
    ) -> ComparisonReportRecord:
        """Submit an operator-requested report and return its acknowledged record."""
        ...

    async def get_temporary_report(
        self,
        evaluation_id: UUID,
    ) -> tuple[ComparisonReportRecord, tuple[ComparisonSimulationRecord, ...]]:
        """Return a report and its simulations; raise LookupError if it is unknown."""
        ...


def _model_json(model: BaseModel) -> JsonObject:
    payload = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    selection = getattr(model, "spm", None)
    if selection is not None:
        # An explicit null information date clears a bundle cutoff. Preserve
        # that distinction while leaving omitted request fields omitted.
        payload["spm"] = selection.model_dump(mode="json", exclude_unset=True)
    return _json_object_adapter.validate_python(payload)


def _response(result: BackendResponse) -> Response:
    headers = {
        **result.headers,
        **BACKEND_RESPONSE_HEADER,
    }
    return Response(
        content=result.content,
        status_code=result.status_code,
        headers=headers,
    )


def _route_template(request: Request) -> str:
    return getattr(request.scope.get("route"), "path", request.url.path)


def _request_identifiers(request: Request) -> RequestIdentifiers:
    identifiers: RequestIdentifiers = {}
    job_id = request.path_params.get("job_id")
    batch_job_id = request.path_params.get("batch_job_id")
    evaluation_id = request.path_params.get("evaluation_id")
    if isinstance(job_id, str):
        identifiers["job_id"] = job_id
    if isinstance(batch_job_id, str):
        identifiers["batch_job_id"] = batch_job_id
    if isinstance(evaluation_id, str):
        identifiers["evaluation_id"] = evaluation_id
    return identifiers


def create_app(
    *,
    settings: Settings | None = None,
    backend: SimulationBackend | None = None,
    auth_dependency: AuthenticationDependency | None = None,
    comparison_backend: ComparisonBackend | None = None,
    simulation_database: SimulationDatabaseConnection | None = None,
    simulation_jobs: SimulationJobService | None = None,
    internal_caller_verifier: InternalCallerVerifier | None = None,
) -> FastAPI:
    """Build the app with injectable auth/backend seams for hermetic tests."""
    runtime_settings = settings or Settings.from_env()
    runtime_database = simulation_database
    if runtime_database is None and runtime_settings.simulation_database_url:
        runtime_database = SimulationDatabase.from_url(
            runtime_settings.simulation_database_url
        )
    runtime_jobs = simulation_jobs
    runtime_collection_queue: CloudTasksCollectionQueue | None = None
    if (
        runtime_jobs is None
        and runtime_settings.simulation_jobs_configured
        and isinstance(runtime_database, SimulationDatabase)
    ):
        runtime_collection_queue = CloudTasksCollectionQueue(
            queue=runtime_settings.reconciliation_queue,
            target_url=runtime_settings.reconciliation_target_url,
            invoker_service_account=(
                runtime_settings.reconciliation_invoker_service_account
            ),
        )
        runtime_jobs = SimulationJobService(
            store=JobStore(runtime_database.engine),
            dispatcher=ModalCallDispatcher(
                runtime_settings.simulation_modal_environment
            ),
            inspector=ModalCallInspector(),
            queue=runtime_collection_queue,
            job_kinds=REGISTERED_JOB_KINDS,
        )
    runtime_verifier = internal_caller_verifier
    if runtime_verifier is None and runtime_settings.simulation_jobs_configured:
        runtime_verifier = GoogleOidcVerifier(
            audience=runtime_settings.reconciliation_target_url,
            service_account_email=(
                runtime_settings.reconciliation_invoker_service_account
            ),
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runtime_settings.validate()
        await runtime_backend.start()
        try:
            yield
        finally:
            await runtime_backend.close()
            if runtime_collection_queue is not None:
                await runtime_collection_queue.close()
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

    def bind_observability_id(request: Request, value: object) -> str | None:
        observability_id = normalize_observability_id(value)
        request.state.observability_id = observability_id
        if observability_id is not None:
            try:
                runtime.set_context(observability_id=observability_id)
            except Exception:  # noqa: BLE001, S110  # telemetry is non-fatal
                pass
        return observability_id

    def start_workflow_observability(request: Request) -> str:
        observability_id = resolve_observability_id(
            getattr(request.state, "incoming_observability_id", None)
        )
        bind_observability_id(request, observability_id)
        return observability_id

    def backend_observability_id(headers: dict[str, str]) -> str | None:
        return next(
            (
                normalize_observability_id(value)
                for key, value in headers.items()
                if key.lower() == OBSERVABILITY_ID_HEADER.lower()
            ),
            None,
        )

    def adopt_backend_observability_id(
        request: Request,
        headers: dict[str, str],
    ) -> str | None:
        current = normalize_observability_id(request.state.observability_id)
        downstream = backend_observability_id(headers)
        if current is not None and downstream is not None and current != downstream:
            try:
                runtime.event(
                    "simulation_entry_observability_id_mismatch",
                    attributes={
                        "backend": "old_gateway",
                        "reason": "downstream_identifier_differed",
                    },
                )
            except Exception:  # noqa: BLE001, S110  # telemetry is non-fatal
                pass
            return current
        return bind_observability_id(request, current or downstream)

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
                    "path": _route_template(request),
                    **_request_identifiers(request),
                },
            )
            response = Response(
                content="Internal Server Error",
                status_code=500,
                media_type="text/plain",
                headers={REQUEST_ID_HEADER: request_id},
            )
        bound_observability_id = normalize_observability_id(
            request.state.observability_id
        )
        if bound_observability_id is not None:
            response.headers[OBSERVABILITY_ID_HEADER] = bound_observability_id
        elif OBSERVABILITY_ID_HEADER in response.headers:
            del response.headers[OBSERVABILITY_ID_HEADER]
        if runtime_settings.revision:
            response.headers["X-PolicyEngine-Simulation-Revision"] = (
                runtime_settings.revision
            )
        return response

    async def forward(
        request: Request,
        method: str,
        path: str,
        body: JsonObject | None = None,
        *,
        identifiers: RequestIdentifiers | None = None,
        response_identifier_key: ResponseIdentifier | None = None,
    ) -> Response:
        backend_started = time.monotonic()
        route = _route_template(request)
        event_identifiers: RequestIdentifiers = identifiers or {}
        try:
            runtime.set_context(backend="old_gateway", **event_identifiers)
        except Exception:  # noqa: BLE001, S110  # telemetry is non-fatal
            pass
        try:
            result = await runtime_backend.request(
                method,
                path,
                json_body=body,
                request_id=request.state.request_id,
                observability_id=request.state.observability_id,
            )
            observability_id = adopt_backend_observability_id(
                request,
                result.headers,
            )
            attributes: BackendTelemetryAttributes = {
                "request_id": request.state.request_id,
                "route": route,
                "method": method,
                "status_code": result.status_code,
                "elapsed_ms": round(
                    (time.monotonic() - backend_started) * 1000,
                    2,
                ),
                **event_identifiers,
            }
            try:
                response_payload = BackendTelemetryPayload.model_validate_json(
                    result.content
                )
            except ValidationError:
                response_payload = None
            job_state = (
                response_payload.status if response_payload is not None else None
            )
            if job_state is not None:
                attributes["job_state"] = job_state
            response_identifier = (
                getattr(response_payload, response_identifier_key)
                if response_payload is not None and response_identifier_key
                else None
            )
            if response_identifier_key and response_identifier is not None:
                attributes[response_identifier_key] = response_identifier
            runtime.event(
                "simulation_entry_backend_response",
                attributes=attributes,
            )
            response = _response(result)
            if observability_id is not None:
                response.headers[OBSERVABILITY_ID_HEADER] = observability_id
            elif OBSERVABILITY_ID_HEADER in response.headers:
                del response.headers[OBSERVABILITY_ID_HEADER]
        except BackendTimeoutError:
            runtime.event(
                "simulation_entry_backend_timeout",
                attributes={
                    "request_id": request.state.request_id,
                    "route": route,
                    **event_identifiers,
                },
            )
            return JSONResponse(
                status_code=504,
                content={"detail": "Simulation backend timed out."},
                headers={
                    "Retry-After": "10",
                    **BACKEND_RESPONSE_HEADER,
                },
            )
        except BackendUnavailableError:
            runtime.event(
                "simulation_entry_backend_unavailable",
                attributes={
                    "request_id": request.state.request_id,
                    "route": route,
                    **event_identifiers,
                },
            )
            return JSONResponse(
                status_code=503,
                content={"detail": "Simulation backend is unavailable."},
                headers={
                    "Retry-After": "10",
                    **BACKEND_RESPONSE_HEADER,
                },
            )
        else:
            return response

    def temporary_submission_payload(
        report: ComparisonReportRecord,
    ) -> TemporaryStage12SubmissionResponse:
        return TemporaryStage12SubmissionResponse(
            evaluation_id=report.evaluation_id,
            status=report.status,
            poll_url=f"/internal/stage12/reports/{report.evaluation_id}",
        )

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
        if not runtime_settings.stage12_enabled:
            return JSONResponse(
                status_code=503,
                content={"detail": "Stage 12 execution is disabled."},
                headers={"Retry-After": "10"},
            )
        comparison = runtime_comparison
        if comparison is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "Stage 12 direct execution is unavailable."},
                headers={"Retry-After": "10"},
            )
        observability_id = start_workflow_observability(request)
        try:
            report = await asyncio.wait_for(
                comparison.submit_temporary_report(
                    request_payload=_model_json(body),
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
                content=temporary_submission_payload(error.report).model_dump(
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
            return JSONResponse(
                status_code=503,
                content={"detail": "Stage 12 direct execution is unavailable."},
                headers={"Retry-After": "10"},
            )
        return JSONResponse(
            status_code=202,
            content=temporary_submission_payload(report).model_dump(mode="json"),
            # The Modal coordinator, rather than Cloud Run, creates the durable
            # parent row. Give it a brief head start before the first status read.
            headers={"Retry-After": "1"},
        )

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
        comparison = runtime_comparison
        if comparison is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "Stage 12 direct execution is unavailable."},
                headers={"Retry-After": "10"},
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
            return JSONResponse(
                status_code=503,
                content={"detail": "Stage 12 report status is unavailable."},
                headers={"Retry-After": "10"},
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
        report_observability_id = bind_observability_id(
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

    def simulation_jobs_unavailable() -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "Simulation jobs are unavailable."},
            headers={"Retry-After": "10"},
        )

    async def internal_caller(
        credentials: HTTPAuthorizationCredentials | None = Depends(_internal_bearer),
    ) -> None:
        """Admit only Cloud Tasks and Cloud Scheduler, by their Google token."""
        verifier = runtime_verifier
        if verifier is None:
            raise HTTPException(status_code=503, headers={"Retry-After": "10"})
        if credentials is None:
            raise HTTPException(status_code=403)
        try:
            await asyncio.to_thread(verifier.verify, credentials.credentials)
        except InternalCallerRejectedError as error:
            logger.info(
                "simulation_job_internal_caller_rejected",
                extra={"reason": str(error)},
            )
            raise HTTPException(status_code=403) from error

    def job_status_payload(view: JobView) -> SimulationJobStatusResponse:
        job = view.job
        return SimulationJobStatusResponse(
            job_id=job.id,
            kind=job.kind,
            status=job.status.value,
            policyengine_version=job.policyengine_version,
            modal_call_id=job.modal_call_id,
            created_at=job.created_at,
            dispatched_at=job.dispatched_at,
            completed_at=job.completed_at,
            error=(
                SimulationJobError(code=job.error_code, message=job.error_message)
                if job.error_code is not None
                else None
            ),
            result=None if view.result is None else view.result.result,
            result_sha256=None if view.result is None else view.result.result_sha256,
        )

    # Simulation job store (API v2 migration, Step 1). These routes are not in
    # the OpenAPI document and no production caller uses them yet. Reading a
    # job uses only the database and never checks or advances its Modal call.
    @app.get(
        "/simulation-jobs/{job_reference}",
        include_in_schema=False,
        dependencies=[Depends(simulation_reader)],
    )
    async def get_simulation_job(job_reference: str, request: Request) -> Response:
        service = runtime_jobs
        if service is None:
            return simulation_jobs_unavailable()
        try:
            view = await service.find(job_reference)
        # A failed database read becomes a 503; the caller polls again.
        except Exception as error:  # noqa: BLE001  # any read failure becomes a 503
            logger.error(  # noqa: TRY400  # the error type is enough; the traceback could expose the job reference's data
                "simulation_job_status_failed",
                extra={
                    "request_id": request.state.request_id,
                    "error_type": type(error).__name__,
                },
            )
            return simulation_jobs_unavailable()
        if view is None:
            return JSONResponse(
                status_code=404,
                content={"detail": "Simulation job was not found."},
            )
        headers: dict[str, str] = {}
        job_observability_id = bind_observability_id(request, view.job.observability_id)
        if job_observability_id is not None:
            headers[OBSERVABILITY_ID_HEADER] = job_observability_id
        if not view.job.finished:
            headers["Retry-After"] = SIMULATION_JOB_RETRY_AFTER_SECONDS
        return JSONResponse(
            status_code=200 if view.job.finished else 202,
            content=job_status_payload(view).model_dump(mode="json"),
            headers=headers or None,
        )

    @app.post(
        "/internal/simulation-jobs/{job_id}/collect",
        include_in_schema=False,
        dependencies=[Depends(internal_caller)],
    )
    async def collect_simulation_job(job_id: UUID, request: Request) -> Response:
        service = runtime_jobs
        if service is None:
            return simulation_jobs_unavailable()
        try:
            report = await service.collect(job_id)
        # Modal, Cloud Tasks or the database failed; a 503 makes Cloud Tasks
        # retry this task with its queue's backoff.
        except Exception as error:  # noqa: BLE001  # every transient failure becomes a 503
            logger.warning(
                "simulation_job_collection_failed",
                extra={
                    "request_id": request.state.request_id,
                    "job_id": str(job_id),
                    "error_type": type(error).__name__,
                },
            )
            return simulation_jobs_unavailable()
        payload = SimulationJobCollectionResponse(
            outcome=report.outcome.value,
            job_status=None if report.job_status is None else report.job_status.value,
            next_check_seconds=report.next_check_seconds,
        )
        return JSONResponse(content=payload.model_dump(mode="json"))

    @app.post(
        "/internal/simulation-jobs/sweep",
        include_in_schema=False,
        dependencies=[Depends(internal_caller)],
    )
    async def sweep_simulation_jobs(request: Request) -> Response:
        service = runtime_jobs
        if service is None:
            return simulation_jobs_unavailable()
        try:
            report = await service.sweep()
        except Exception as error:  # noqa: BLE001  # a failed sweep becomes a 503; the next run repeats it
            logger.warning(
                "simulation_job_sweep_failed",
                extra={
                    "request_id": request.state.request_id,
                    "error_type": type(error).__name__,
                },
            )
            return simulation_jobs_unavailable()
        payload = SimulationJobSweepResponse(
            abandoned=report.abandoned,
            enqueued=report.enqueued,
            enqueue_failures=report.enqueue_failures,
        )
        # Report refused tasks as a failed run, so Cloud Scheduler shows it.
        return JSONResponse(
            status_code=503 if report.enqueue_failures else 200,
            content=payload.model_dump(mode="json"),
        )

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
        dependencies=[Depends(simulation_submitter)],
    )
    async def submit_comparison(
        body: SimulationRequest,
        request: Request,
    ) -> Response:
        observability_id = start_workflow_observability(request)
        request_payload = _model_json(body)
        response = await forward(
            request,
            "POST",
            "/simulate/economy/comparison",
            request_payload,
            response_identifier_key="job_id",
        )
        if (
            runtime_settings.stage12_enabled
            and runtime_comparison is not None
            and response.status_code in {200, 202}
        ):
            # Wait only for Modal to acknowledge the already-deployed report
            # coordinator invocation. Calculation and comparison run later.
            try:
                await asyncio.wait_for(
                    runtime_comparison.dispatch_after_production(
                        request_payload=request_payload,
                        production_response=bytes(response.body),
                        request_id=request.state.request_id,
                        observability_id=observability_id,
                    ),
                    timeout=STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.error(  # noqa: TRY400  # an expected timeout, fully described by timeout_seconds; the traceback would show only asyncio cancellation
                    "stage12_comparison_dispatch_timed_out",
                    extra={
                        "request_id": request.state.request_id,
                        "timeout_seconds": STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS,
                    },
                )
            # Automatic Stage 12 work must never change the production v1
            # response, including for unexpected integration failures.
            except Exception as error:  # noqa: BLE001  # Stage 12 dispatch must never change the v1 response
                logger.error(  # noqa: TRY400  # Stage 12 failure logs carry only the error type; a traceback could expose request or calculation data
                    "stage12_comparison_dispatch_failed",
                    extra={
                        "request_id": request.state.request_id,
                        "error_type": type(error).__name__,
                    },
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
        dependencies=[Depends(simulation_submitter)],
    )
    async def submit_budget_window(
        body: BudgetWindowBatchRequest,
        request: Request,
    ) -> Response:
        start_workflow_observability(request)
        return await forward(
            request,
            "POST",
            "/simulate/economy/budget-window",
            _model_json(body),
            response_identifier_key="batch_job_id",
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
        dependencies=[Depends(simulation_reader)],
    )
    async def get_job(job_id: str, request: Request) -> Response:
        return await forward(
            request,
            "GET",
            f"/jobs/{job_id}",
            identifiers={"job_id": job_id},
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
        dependencies=[Depends(simulation_reader)],
    )
    async def get_budget_window_job(
        batch_job_id: str,
        request: Request,
    ) -> Response:
        return await forward(
            request,
            "GET",
            f"/budget-window-jobs/{batch_job_id}",
            identifiers={"batch_job_id": batch_job_id},
        )

    @app.get(
        "/versions",
        summary="List Versions",
        description="List all available routing versions.",
        operation_id="list_versions_versions_get",
        response_model=VersionsResponse,
    )
    async def list_versions(request: Request) -> Response:
        return await forward(request, "GET", "/versions")

    @app.get(
        "/versions/{kind}",
        summary="Get Country Versions",
        description="Get available versions for policyengine, US, or UK routing.",
        operation_id="get_country_versions_versions__kind__get",
        response_model=VersionMap,
    )
    async def get_country_versions(kind: str, request: Request) -> Response:
        return await forward(request, "GET", f"/versions/{kind}")

    @app.get(
        "/health",
        description="Health check endpoint.",
        operation_id="health_health_get",
        response_model=HealthResponse,
    )
    async def health() -> HealthResponse:
        return HealthResponse()

    async def simulation_database_readiness() -> SimulationDatabaseReadiness | None:
        if runtime_database is None:
            return None
        try:
            revision = await asyncio.wait_for(
                runtime_database.read_revision(),
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

    @app.get("/ready", response_model=SimulationEntrypointReadinessResponse)
    async def ready() -> Response:
        backend_ready = await runtime_backend.ready()
        readiness = SimulationEntrypointReadinessResponse(
            status="ready" if backend_ready else "not_ready",
            simulation_database=await simulation_database_readiness(),
        )
        # Without a configured database the response keeps its old shape.
        return JSONResponse(
            readiness.model_dump(
                mode="json",
                exclude={"simulation_database"} if runtime_database is None else None,
            ),
            status_code=200 if backend_ready else 503,
        )

    @app.post(
        "/ping",
        description="Verify the API is able to receive and process requests.",
        operation_id="ping_ping_post",
        response_model=PingResponse,
    )
    async def ping(body: PingRequest, request: Request) -> Response:
        return await forward(request, "POST", "/ping", _model_json(body))

    return app


app = create_app()
