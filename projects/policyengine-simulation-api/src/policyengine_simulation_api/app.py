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


def _model_json(model: Any) -> Mapping[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def _response(result: BackendResponse) -> Response:
    headers = {
        **result.headers,
        "X-PolicyEngine-Simulation-Backend": "old_gateway",
    }
    return Response(
        content=result.content,
        status_code=result.status_code,
        headers=headers,
    )


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
                "path": request.url.path,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
                "backend": "old_gateway",
            },
        )
        return response

    async def forward(
        request: Request,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
    ) -> Response:
        backend_started = time.monotonic()
        try:
            result = await runtime_backend.request(
                method,
                path,
                json_body=body,
                request_id=request.state.request_id,
            )
            attributes: dict[str, Any] = {
                "request_id": request.state.request_id,
                "route": path,
                "method": method,
                "status_code": result.status_code,
                "elapsed_ms": round(
                    (time.monotonic() - backend_started) * 1000,
                    2,
                ),
            }
            try:
                job_state = json.loads(result.content).get("status")
            except (AttributeError, json.JSONDecodeError, UnicodeDecodeError):
                job_state = None
            if isinstance(job_state, str):
                attributes["job_state"] = job_state
            record_event("simulation_api_backend_response", **attributes)
            return _response(result)
        except BackendTimeout:
            record_event(
                "simulation_api_backend_timeout",
                request_id=request.state.request_id,
                route=path,
            )
            return JSONResponse(
                status_code=504,
                content={"detail": "Simulation backend timed out."},
                headers={"Retry-After": "10"},
            )
        except (BackendUnavailable, BackendAuthenticationError):
            record_event(
                "simulation_api_backend_unavailable",
                request_id=request.state.request_id,
                route=path,
            )
            return JSONResponse(
                status_code=503,
                content={"detail": "Simulation backend is unavailable."},
                headers={"Retry-After": "10"},
            )

    protected = [Depends(authenticate)]

    @app.post(
        "/simulate/economy/comparison",
        operation_id="submit_simulation_simulate_economy_comparison_post",
        response_model=JobSubmitResponse,
        response_model_exclude_none=True,
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
        )

    @app.post(
        "/simulate/economy/budget-window",
        operation_id=("submit_budget_window_batch_simulate_economy_budget_window_post"),
        response_model=BudgetWindowBatchSubmitResponse,
        response_model_exclude_none=True,
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
        )

    @app.get(
        "/jobs/{job_id}",
        operation_id="get_job_status_jobs__job_id__get",
        response_model=JobStatusResponse,
        response_model_exclude_none=True,
        dependencies=protected,
    )
    async def get_job(job_id: str, request: Request) -> Response:
        return await forward(request, "GET", f"/jobs/{job_id}")

    @app.get(
        "/budget-window-jobs/{batch_job_id}",
        operation_id=(
            "get_budget_window_job_status_budget_window_jobs__batch_job_id__get"
        ),
        response_model=BudgetWindowBatchStatusResponse,
        response_model_exclude_none=True,
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
        )

    @app.get("/versions", operation_id="list_versions_versions_get")
    async def versions(request: Request) -> Response:
        return await forward(request, "GET", "/versions")

    @app.get(
        "/versions/{kind}",
        operation_id="get_country_versions_versions__kind__get",
    )
    async def versions_by_kind(kind: str, request: Request) -> Response:
        return await forward(request, "GET", f"/versions/{kind}")

    @app.get("/health", operation_id="health_health_get")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/ready")
    async def ready() -> Response:
        if await runtime_backend.ready():
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "not_ready"}, status_code=503)

    @app.post(
        "/ping",
        operation_id="ping_ping_post",
        response_model=PingResponse,
    )
    async def ping(body: PingRequest, request: Request) -> Response:
        return await forward(request, "POST", "/ping", _model_json(body))

    return app


app = create_app()
