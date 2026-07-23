"""FastAPI application for the Cloud Run Simulation API."""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable, Mapping

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from policyengine_observability import record_event
from policyengine_simulation_contract.gateway_models import (
    BudgetWindowBatchRequest,
    BudgetWindowBatchStatusResponse,
    BudgetWindowBatchSubmitResponse,
    JobStatusResponse,
    JobSubmitResponse,
    PingRequest,
    PingResponse,
    SimulationRequest,
)
from policyengine_simulation_observability.observability import (
    configure_process_observability,
    init_simulation_observability,
)

from policyengine_simulation_api.auth import CallerAuthenticator
from policyengine_simulation_api.backend import (
    BackendAuthenticationError,
    BackendResponse,
    BackendTimeout,
    BackendUnavailable,
    OldGatewayBackend,
    SimulationBackend,
)
from policyengine_simulation_api.config import Settings


logger = logging.getLogger(__name__)
BACKEND_RESPONSE_HEADER = {
    "X-PolicyEngine-Simulation-Backend": "old_gateway",
}


def _model_json(model: Any) -> Mapping[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


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


def _request_identifiers(request: Request) -> dict[str, str]:
    return {
        key: value
        for key in ("job_id", "batch_job_id")
        if isinstance((value := request.path_params.get(key)), str)
    }


def create_app(
    *,
    settings: Settings | None = None,
    backend: SimulationBackend | None = None,
    auth_dependency: Callable[..., Any] | None = None,
) -> FastAPI:
    """Build the app with injectable auth/backend seams for hermetic tests."""

    runtime_settings = settings or Settings.from_env()
    runtime_backend = backend or OldGatewayBackend(runtime_settings)
    authenticate = auth_dependency or CallerAuthenticator(runtime_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime_settings.validate()
        await runtime_backend.start()
        try:
            yield
        finally:
            await runtime_backend.close()

    app = FastAPI(
        title="PolicyEngine Simulation API",
        description=("Authenticated simulation submission and polling control plane."),
        version="1.0.0",
        lifespan=lifespan,
    )

    configure_process_observability(
        platform="cloud_run",
        service_role="simulation_api",
    )
    init_simulation_observability(app, service_role="simulation_api")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.monotonic()
        response = await call_next(request)
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "simulation_api_request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": _route_template(request),
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
                "backend": "old_gateway",
                **_request_identifiers(request),
            },
        )
        return response

    async def forward(
        request: Request,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        identifiers: Mapping[str, str] | None = None,
        response_identifier_key: str | None = None,
    ) -> Response:
        backend_started = time.monotonic()
        route = _route_template(request)
        event_identifiers = dict(identifiers or {})
        try:
            result = await runtime_backend.request(
                method,
                path,
                json_body=body,
                request_id=request.state.request_id,
            )
            attributes: dict[str, Any] = {
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
                response_payload = json.loads(result.content)
            except (AttributeError, json.JSONDecodeError, UnicodeDecodeError):
                response_payload = None
            job_state = (
                response_payload.get("status")
                if isinstance(response_payload, dict)
                else None
            )
            if isinstance(job_state, str):
                attributes["job_state"] = job_state
            response_identifier = (
                response_payload.get(response_identifier_key)
                if isinstance(response_payload, dict) and response_identifier_key
                else None
            )
            if response_identifier_key and isinstance(response_identifier, str):
                attributes[response_identifier_key] = response_identifier
            record_event("simulation_api_backend_response", **attributes)
            return _response(result)
        except BackendTimeout:
            record_event(
                "simulation_api_backend_timeout",
                request_id=request.state.request_id,
                route=route,
                **event_identifiers,
            )
            return JSONResponse(
                status_code=504,
                content={"detail": "Simulation backend timed out."},
                headers={
                    "Retry-After": "10",
                    **BACKEND_RESPONSE_HEADER,
                },
            )
        except (BackendUnavailable, BackendAuthenticationError):
            record_event(
                "simulation_api_backend_unavailable",
                request_id=request.state.request_id,
                route=route,
                **event_identifiers,
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
            400: {"description": "Invalid request (unknown country/version)"},
        },
        dependencies=protected,
    )
    async def submit_comparison(
        body: SimulationRequest,
        request: Request,
    ) -> Response:
        return await forward(
            request,
            "POST",
            "/simulate/economy/comparison",
            _model_json(body),
            response_identifier_key="job_id",
        )

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
            400: {"description": "Invalid request (unknown country/version/year)"},
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
    )
    async def list_versions(request: Request) -> dict:
        return await forward(request, "GET", "/versions")

    @app.get(
        "/versions/{kind}",
        summary="Get Country Versions",
        description="Get available versions for policyengine, US, or UK routing.",
        operation_id="get_country_versions_versions__kind__get",
    )
    async def get_country_versions(kind: str, request: Request) -> dict:
        return await forward(request, "GET", f"/versions/{kind}")

    @app.get(
        "/health",
        description="Health check endpoint.",
        operation_id="health_health_get",
    )
    async def health() -> dict:
        return {"status": "healthy"}

    @app.get("/ready")
    async def ready() -> Response:
        if await runtime_backend.ready():
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not_ready"}, status_code=503)

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
