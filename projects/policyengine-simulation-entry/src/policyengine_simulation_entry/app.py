"""FastAPI application for the Cloud Run Simulation Entrypoint."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, Literal, Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, TypeAdapter, ValidationError
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
    ReadinessResponse,
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
from policyengine_simulation_observability.observability import (
    init_simulation_observability,
)
from policyengine_simulation_observability.identifiers import (
    OBSERVABILITY_ID_HEADER,
    generate_observability_id,
    normalize_observability_id,
)
from starlette.datastructures import MutableHeaders

from policyengine_simulation_entry.auth import CallerAuthenticator
from policyengine_simulation_entry.backend import (
    BackendResponse,
    BackendTimeout,
    BackendUnavailable,
    OldGatewayBackend,
    SimulationBackend,
)
from policyengine_simulation_entry.config import Settings
from policyengine_simulation_entry.schemas import (
    BackendTelemetryAttributes,
    BackendTelemetryPayload,
    CallerIdentity,
    RequestIdentifiers,
    TemporaryStage12ReportResponse,
    TemporaryStage12SubmissionResponse,
)
from policyengine_simulation_entry.stage12_backend import (
    TemporaryStage12DispatchFailed,
    TemporaryStage12UnsupportedRequest,
)

logger = logging.getLogger(__name__)
BACKEND_RESPONSE_HEADER = {
    "X-PolicyEngine-Simulation-Backend": "old_gateway",
}
_json_object_adapter = TypeAdapter(JsonObject)
type AuthenticationDependency = Callable[[], CallerIdentity | None]
type ResponseIdentifier = Literal["job_id", "batch_job_id"]
STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS = 5.0


class ComparisonBackend(Protocol):
    async def dispatch_after_production(
        self,
        *,
        request_payload: dict[str, Any],
        production_response: bytes,
        request_id: str,
        observability_id: str | None = None,
    ) -> None: ...

    async def submit_temporary_report(
        self,
        *,
        request_payload: dict[str, Any],
        request_id: str,
        observability_id: str | None = None,
    ) -> ComparisonReportRecord: ...

    async def get_temporary_report(
        self,
        evaluation_id: UUID,
    ) -> tuple[ComparisonReportRecord, tuple[ComparisonSimulationRecord, ...]]: ...


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
) -> FastAPI:
    """Build the app with injectable auth/backend seams for hermetic tests."""

    runtime_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime_settings.validate()
        await runtime_backend.start()
        try:
            yield
        finally:
            await runtime_backend.close()
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
    runtime_comparison = comparison_backend
    if runtime_comparison is None and runtime_settings.stage12_resources_configured:
        from policyengine_simulation_entry.stage12_backend import (
            Stage12ComparisonBackend,
        )

        runtime_comparison = Stage12ComparisonBackend.from_settings(
            runtime_settings,
            runtime=runtime,
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        headers = MutableHeaders(scope=request.scope)
        request_id = headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        observability_id = (
            normalize_observability_id(headers.get(OBSERVABILITY_ID_HEADER))
            or generate_observability_id()
        )
        headers[REQUEST_ID_HEADER] = request_id
        headers[OBSERVABILITY_ID_HEADER] = observability_id
        request.state.request_id = request_id
        request.state.observability_id = observability_id
        try:
            runtime.set_context(
                request_id=request_id,
                observability_id=observability_id,
            )
        except Exception:
            pass
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
        if OBSERVABILITY_ID_HEADER not in response.headers:
            response.headers[OBSERVABILITY_ID_HEADER] = observability_id
        if runtime_settings.revision:
            response.headers["X-PolicyEngine-Simulation-Revision"] = (
                runtime_settings.revision
            )
        try:
            runtime.set_context(backend="old_gateway", **_request_identifiers(request))
        except Exception:
            pass
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
            result = await runtime_backend.request(
                method,
                path,
                json_body=body,
                request_id=request.state.request_id,
                observability_id=request.state.observability_id,
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
            return _response(result)
        except BackendTimeout:
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
        except BackendUnavailable:
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

    protected = [Depends(authenticate)]

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
        dependencies=protected,
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
        try:
            report = await asyncio.wait_for(
                comparison.submit_temporary_report(
                    request_payload=_model_json(body),
                    request_id=request.state.request_id,
                    observability_id=request.state.observability_id,
                ),
                timeout=STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return JSONResponse(
                status_code=504,
                content={"detail": "Stage 12 submission acknowledgement timed out."},
                headers={"Retry-After": "10"},
            )
        except TemporaryStage12UnsupportedRequest as error:
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
        except Exception as error:  # noqa: BLE001
            logger.error(
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
        dependencies=protected,
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
        except Exception as error:  # noqa: BLE001
            logger.error(
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
        if report.observability_id is not None:
            response_headers[OBSERVABILITY_ID_HEADER] = report.observability_id
            try:
                runtime.set_context(observability_id=report.observability_id)
            except Exception:
                pass
        return JSONResponse(
            status_code=202 if running else 200,
            content=payload.model_dump(mode="json"),
            headers=response_headers or None,
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
        dependencies=protected,
    )
    async def submit_comparison(
        body: SimulationRequest,
        request: Request,
    ) -> Response:
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
                        observability_id=request.state.observability_id,
                    ),
                    timeout=STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.error(
                    "stage12_comparison_dispatch_timed_out",
                    extra={
                        "request_id": request.state.request_id,
                        "timeout_seconds": STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS,
                    },
                )
            # Automatic Stage 12 work must never change the production v1
            # response, including for unexpected integration failures.
            except Exception as error:  # noqa: BLE001
                logger.error(
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
        dependencies=protected,
    )
    async def submit_budget_window(
        body: BudgetWindowBatchRequest,
        request: Request,
    ) -> Response:
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
        dependencies=protected,
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
        dependencies=protected,
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

    @app.get("/ready", response_model=ReadinessResponse)
    async def ready() -> Response:
        if await runtime_backend.ready():
            return JSONResponse(ReadinessResponse(status="ready").model_dump())
        return JSONResponse(
            ReadinessResponse(status="not_ready").model_dump(),
            status_code=503,
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
