"""FastAPI application for the Cloud Run Simulation Entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol
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
from policyengine_simulation_observability.identifiers import (
    OBSERVABILITY_ID_HEADER,
    normalize_observability_id,
    resolve_observability_id,
)
from policyengine_simulation_observability.observability import (
    init_simulation_observability,
)
from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.datastructures import MutableHeaders

from policyengine_simulation_entry.auth import (
    AuthenticationDependency,
    AuthorizationDependency,
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
from policyengine_simulation_entry.schemas import (
    BackendTelemetryAttributes,
    BackendTelemetryPayload,
    RequestIdentifiers,
    TemporaryStage12ReportResponse,
    TemporaryStage12SubmissionResponse,
)
from policyengine_simulation_entry.stage12_backend import (
    Stage12ComparisonBackend,
    TemporaryStage12DispatchFailed,
    TemporaryStage12UnsupportedRequestError,
)
from policyengine_simulation_entry.v2_versions import (
    ModalV2VersionsSource,
    UnknownVersionKindError,
    V2VersionsSource,
    is_version_kind,
    version_map_from_manifest,
    versions_from_manifest,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.stage12_manifest import V2VersionManifest
    from starlette.middleware.base import RequestResponseEndpoint

logger = logging.getLogger(__name__)
BACKEND_RESPONSE_HEADER = {
    "X-PolicyEngine-Simulation-Backend": "old_gateway",
}
_json_object_adapter = TypeAdapter(JsonObject)
type ResponseIdentifier = Literal["job_id", "batch_job_id"]
STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS = 5.0
# How long the internal v2 version routes wait for the manifest's Modal Dict.
V2_MANIFEST_READ_TIMEOUT_SECONDS = 5.0
V2_MANIFEST_UNAVAILABLE_DETAIL = "The v2 version manifest is unavailable."


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


@dataclass(frozen=True)
class _BackendCall:
    """One request a route forwards to the old gateway, and what its event records.

    ``identifiers`` come from the route's path; ``response_identifier_key``
    names the response field to copy into the backend response event.
    """

    method: str
    path: str
    body: JsonObject | None = None
    identifiers: RequestIdentifiers | None = None
    response_identifier_key: ResponseIdentifier | None = None


class _BackendFailure(NamedTuple):
    """The event and sanitized response for an old-gateway call that failed."""

    event_name: str
    status_code: int
    detail: str


_BACKEND_TIMEOUT = _BackendFailure(
    "simulation_entry_backend_timeout", 504, "Simulation backend timed out."
)
_BACKEND_UNAVAILABLE = _BackendFailure(
    "simulation_entry_backend_unavailable", 503, "Simulation backend is unavailable."
)


def _backend_observability_id(headers: dict[str, str]) -> str | None:
    """Return the backend's normalized observability ID header, if it sent one."""
    return next(
        (
            normalize_observability_id(value)
            for key, value in headers.items()
            if key.lower() == OBSERVABILITY_ID_HEADER.lower()
        ),
        None,
    )


def _set_observability_header(response: Response, observability_id: str | None) -> None:
    """Return ``observability_id`` on the response, or drop any stale header."""
    if observability_id is not None:
        response.headers[OBSERVABILITY_ID_HEADER] = observability_id
    elif OBSERVABILITY_ID_HEADER in response.headers:
        del response.headers[OBSERVABILITY_ID_HEADER]


def _add_response_telemetry(
    attributes: BackendTelemetryAttributes,
    content: bytes,
    response_identifier_key: ResponseIdentifier | None,
) -> None:
    """Add the job state and requested identifier from a JSON backend response."""
    try:
        response_payload = BackendTelemetryPayload.model_validate_json(content)
    except ValidationError:
        response_payload = None
    job_state = response_payload.status if response_payload is not None else None
    if job_state is not None:
        attributes["job_state"] = job_state
    response_identifier = (
        getattr(response_payload, response_identifier_key)
        if response_payload is not None and response_identifier_key
        else None
    )
    if response_identifier_key and response_identifier is not None:
        attributes[response_identifier_key] = response_identifier


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


class _EntryRuntime:
    """The settings, telemetry runtime and backends the entry routes share."""

    def __init__(
        self,
        *,
        settings: Settings,
        runtime: ObservabilityRuntime,
        backend: SimulationBackend,
        comparison: ComparisonBackend | None,
    ) -> None:
        """Keep the dependencies that ``create_app`` resolved."""
        self.settings = settings
        self.runtime = runtime
        self.backend = backend
        self.comparison = comparison

    def bind_observability_id(self, request: Request, value: object) -> str | None:
        """Bind a normalized observability ID to the request and telemetry context."""
        observability_id = normalize_observability_id(value)
        request.state.observability_id = observability_id
        if observability_id is not None:
            # Telemetry is non-fatal.
            with contextlib.suppress(Exception):
                self.runtime.set_context(observability_id=observability_id)
        return observability_id

    def start_workflow_observability(self, request: Request) -> str:
        """Bind the caller's observability ID, or a new one, to a workflow request."""
        observability_id = resolve_observability_id(
            getattr(request.state, "incoming_observability_id", None)
        )
        self.bind_observability_id(request, observability_id)
        return observability_id

    def adopt_backend_observability_id(
        self,
        request: Request,
        headers: dict[str, str],
    ) -> str | None:
        """Keep the entry's observability ID, else adopt the backend's."""
        current = normalize_observability_id(request.state.observability_id)
        downstream = _backend_observability_id(headers)
        if current is not None and downstream is not None and current != downstream:
            # Telemetry is non-fatal.
            with contextlib.suppress(Exception):
                self.runtime.event(
                    "simulation_entry_observability_id_mismatch",
                    attributes={
                        "backend": "old_gateway",
                        "reason": "downstream_identifier_differed",
                    },
                )
            return current
        return self.bind_observability_id(request, current or downstream)

    async def forward(self, request: Request, call: _BackendCall) -> Response:
        """Forward ``call`` to the old gateway and relay its response.

        Timeouts and an unavailable gateway become sanitized 504 and 503
        responses; every outcome is reported as a telemetry event.
        """
        backend_started = time.monotonic()
        route = _route_template(request)
        event_identifiers: RequestIdentifiers = call.identifiers or {}
        # Telemetry is non-fatal.
        with contextlib.suppress(Exception):
            self.runtime.set_context(backend="old_gateway", **event_identifiers)
        try:
            result = await self.backend.request(
                call.method,
                call.path,
                json_body=call.body,
                request_id=request.state.request_id,
                observability_id=request.state.observability_id,
            )
            observability_id = self.adopt_backend_observability_id(
                request,
                result.headers,
            )
            attributes: BackendTelemetryAttributes = {
                "request_id": request.state.request_id,
                "route": route,
                "method": call.method,
                "status_code": result.status_code,
                "elapsed_ms": round(
                    (time.monotonic() - backend_started) * 1000,
                    2,
                ),
                **event_identifiers,
            }
            _add_response_telemetry(
                attributes, result.content, call.response_identifier_key
            )
            self.runtime.event(
                "simulation_entry_backend_response",
                attributes=attributes,
            )
            response = _response(result)
            _set_observability_header(response, observability_id)
        except BackendTimeoutError:
            return self._backend_failure_response(
                request, _BACKEND_TIMEOUT, route=route, identifiers=event_identifiers
            )
        except BackendUnavailableError:
            return self._backend_failure_response(
                request,
                _BACKEND_UNAVAILABLE,
                route=route,
                identifiers=event_identifiers,
            )
        else:
            return response

    def _backend_failure_response(
        self,
        request: Request,
        failure: _BackendFailure,
        *,
        route: str,
        identifiers: RequestIdentifiers,
    ) -> JSONResponse:
        """Report a failed old-gateway call and return its sanitized response."""
        self.runtime.event(
            failure.event_name,
            attributes={
                "request_id": request.state.request_id,
                "route": route,
                **identifiers,
            },
        )
        return JSONResponse(
            status_code=failure.status_code,
            content={"detail": failure.detail},
            headers={
                "Retry-After": "10",
                **BACKEND_RESPONSE_HEADER,
            },
        )

    async def dispatch_comparison_run(
        self,
        request: Request,
        request_payload: JsonObject,
        response: Response,
        observability_id: str,
    ) -> None:
        """Start a Stage 12 comparison of an accepted submission, if enabled.

        Waits only for Modal to acknowledge the already-deployed report
        coordinator; a timeout or failure is logged with no request data and
        never changes the production response.
        """
        comparison = self.comparison
        if not (
            self.settings.stage12_enabled
            and comparison is not None
            and response.status_code in {200, 202}
        ):
            return
        try:
            await asyncio.wait_for(
                comparison.dispatch_after_production(
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
        _set_observability_header(
            response, normalize_observability_id(request.state.observability_id)
        )
        if settings.revision:
            response.headers["X-PolicyEngine-Simulation-Revision"] = settings.revision
        return response


def _register_temporary_stage12_submission(
    app: FastAPI,
    entry: _EntryRuntime,
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
    entry: _EntryRuntime,
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


def _v2_manifest_unavailable_response() -> JSONResponse:
    """Return the 503 the internal v2 version routes send without a manifest."""
    return JSONResponse(
        status_code=503,
        content={"detail": V2_MANIFEST_UNAVAILABLE_DETAIL},
        headers={"Retry-After": "10"},
    )


def _register_v2_version_routes(
    app: FastAPI,
    versions_source: V2VersionsSource | None,
) -> None:
    """Add the internal routes that answer ``/versions`` from the v2 manifest.

    They answer as ``GET /versions`` and ``GET /versions/{kind}`` would from
    the v2 version manifest, so a staging deploy can compare those answers
    with the old gateway's while ``/versions`` still forwards to it. Like
    ``/versions`` they are public, and they are left out of OpenAPI. They
    never call the old gateway.
    """

    async def read_manifest(request: Request) -> V2VersionManifest | None:
        if versions_source is None:
            return None
        try:
            return await asyncio.wait_for(
                versions_source.load_manifest(),
                timeout=V2_MANIFEST_READ_TIMEOUT_SECONDS,
            )
        # A missing, invalid or unreachable manifest, or a timeout, becomes a
        # 503 rather than an unhandled error.
        except Exception as error:  # noqa: BLE001  # every manifest read failure becomes a 503
            logger.error(  # noqa: TRY400  # the error type describes the failure; a traceback adds only Modal client internals
                "v2_version_manifest_read_failed",
                extra={
                    "request_id": request.state.request_id,
                    "error_type": type(error).__name__,
                },
            )
            return None

    @app.get("/internal/v2/versions", include_in_schema=False)
    async def list_v2_manifest_versions(request: Request) -> Response:
        manifest = await read_manifest(request)
        if manifest is None:
            return _v2_manifest_unavailable_response()
        return JSONResponse(versions_from_manifest(manifest).model_dump(mode="json"))

    @app.get("/internal/v2/versions/{kind}", include_in_schema=False)
    async def get_v2_manifest_kind_versions(kind: str, request: Request) -> Response:
        # As in the old gateway, an unknown kind is a 404 whether or not the
        # manifest can be read.
        if not is_version_kind(kind):
            return JSONResponse(
                status_code=404,
                content={"detail": UnknownVersionKindError(kind).detail},
            )
        manifest = await read_manifest(request)
        if manifest is None:
            return _v2_manifest_unavailable_response()
        return JSONResponse(
            version_map_from_manifest(manifest, kind).model_dump(mode="json")
        )


def _register_simulation_routes(
    app: FastAPI,
    entry: _EntryRuntime,
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
        request_payload = _model_json(body)
        response = await entry.forward(
            request,
            _BackendCall(
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
            _BackendCall(
                "POST",
                "/simulate/economy/budget-window",
                _model_json(body),
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
            _BackendCall("GET", f"/jobs/{job_id}", identifiers={"job_id": job_id}),
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
            _BackendCall(
                "GET",
                f"/budget-window-jobs/{batch_job_id}",
                identifiers={"batch_job_id": batch_job_id},
            ),
        )


def _register_service_routes(app: FastAPI, entry: _EntryRuntime) -> None:
    """Add the public version, health, readiness and ping routes."""

    @app.get(
        "/versions",
        summary="List Versions",
        description="List all available routing versions.",
        operation_id="list_versions_versions_get",
        response_model=VersionsResponse,
    )
    async def list_versions(request: Request) -> Response:
        return await entry.forward(request, _BackendCall("GET", "/versions"))

    @app.get(
        "/versions/{kind}",
        summary="Get Country Versions",
        description="Get available versions for policyengine, US, or UK routing.",
        operation_id="get_country_versions_versions__kind__get",
        response_model=VersionMap,
    )
    async def get_country_versions(kind: str, request: Request) -> Response:
        return await entry.forward(request, _BackendCall("GET", f"/versions/{kind}"))

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
        if await entry.backend.ready():
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
    async def ping(body: PingRequest) -> PingResponse:
        # The entrypoint answers itself, exactly as the old gateway does: the
        # request's value plus one. The old gateway is not called.
        return PingResponse(incremented=body.value + 1)


def create_app(
    *,
    settings: Settings | None = None,
    backend: SimulationBackend | None = None,
    auth_dependency: AuthenticationDependency | None = None,
    comparison_backend: ComparisonBackend | None = None,
    v2_versions_source: V2VersionsSource | None = None,
) -> FastAPI:
    """Build the app with injectable auth/backend seams for hermetic tests.

    Without ``v2_versions_source``, the internal v2 version routes read the
    manifest Dict the settings name, when they name one.
    """
    runtime_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
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
    entry = _EntryRuntime(
        settings=runtime_settings,
        runtime=runtime,
        backend=runtime_backend,
        comparison=runtime_comparison,
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
    runtime_versions_source = v2_versions_source
    if runtime_versions_source is None:
        runtime_versions_source = ModalV2VersionsSource.from_settings(runtime_settings)
    _register_v2_version_routes(app, runtime_versions_source)
    return app


app = create_app()
