"""The entrypoint's backend calls, response telemetry and comparison dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol

from fastapi.responses import JSONResponse, Response
from policyengine_simulation_contract.json_types import JsonObject
from policyengine_simulation_observability.identifiers import (
    OBSERVABILITY_ID_HEADER,
    normalize_observability_id,
    resolve_observability_id,
)
from pydantic import BaseModel, TypeAdapter, ValidationError

from policyengine_simulation_entry.backend import (
    BackendResponse,
    BackendTimeoutError,
    BackendUnavailableError,
    SimulationBackend,
)
from policyengine_simulation_entry.schemas import (
    BackendTelemetryAttributes,
    BackendTelemetryPayload,
    RequestIdentifiers,
)

if TYPE_CHECKING:
    from uuid import UUID

    from fastapi import Request
    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.stage12_execution import (
        ComparisonReportRecord,
        ComparisonSimulationRecord,
    )

    from policyengine_simulation_entry.config import Settings

# Moved out of app.py; this code still logs under that module's name.
logger = logging.getLogger("policyengine_simulation_entry.app")

BACKEND_RESPONSE_HEADER = {
    "X-PolicyEngine-Simulation-Backend": "old_gateway",
}
_json_object_adapter = TypeAdapter(JsonObject)
type ResponseIdentifier = Literal["job_id", "batch_job_id"]
STAGE12_MODAL_SUBMISSION_TIMEOUT_SECONDS = 5.0


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


def model_json(model: BaseModel) -> JsonObject:
    """Return a request model as the JSON object the backends receive."""
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


def route_template(request: Request) -> str:
    """Return the matched route's path template, or the request path."""
    return getattr(request.scope.get("route"), "path", request.url.path)


def request_identifiers(request: Request) -> RequestIdentifiers:
    """Return the job, batch and evaluation IDs in the request's path."""
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
class BackendCall:
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


def set_observability_header(response: Response, observability_id: str | None) -> None:
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


class EntryRuntime:
    """The settings, telemetry runtime and backends the entry routes share."""

    def __init__(
        self,
        *,
        settings: Settings,
        runtime: ObservabilityRuntime,
        backend: SimulationBackend,
        comparison: ComparisonBackend | None,
        database: SimulationDatabaseConnection | None,
    ) -> None:
        """Keep the dependencies that ``create_app`` resolved."""
        self.settings = settings
        self.runtime = runtime
        self.backend = backend
        self.comparison = comparison
        self.database = database

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

    async def forward(self, request: Request, call: BackendCall) -> Response:
        """Forward ``call`` to the old gateway and relay its response.

        Timeouts and an unavailable gateway become sanitized 504 and 503
        responses; every outcome is reported as a telemetry event.
        """
        backend_started = time.monotonic()
        route = route_template(request)
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
            set_observability_header(response, observability_id)
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
