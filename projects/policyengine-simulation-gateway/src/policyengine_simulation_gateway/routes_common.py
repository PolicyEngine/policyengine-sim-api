"""The router, logger and request helpers the gateway's route modules share."""

import contextlib
import logging

from fastapi import APIRouter, Request
from policyengine_observability import ObservabilityRuntime
from policyengine_simulation_observability.identifiers import (
    OBSERVABILITY_ID_HEADER,
    normalize_observability_id,
)

# The routes moved out of endpoints.py, and still log under its name.
logger = logging.getLogger("policyengine_simulation_gateway.endpoints")

router = APIRouter()


def observability_runtime(request: Request) -> ObservabilityRuntime:
    """Return the observability runtime the app was built with."""
    runtime = getattr(request.app.state, "policyengine_observability", None)
    if not isinstance(runtime, ObservabilityRuntime):
        msg = "Simulation gateway observability is not configured"
        raise RuntimeError(msg)  # noqa: TRY004  # a missing runtime is a configuration error, not a bad argument; FastAPI handlers surface it unchanged
    return runtime


def bind_observability_id(
    http_request: Request,
    runtime: ObservabilityRuntime,
    value: object,
) -> str | None:
    """Store a normalized observability id on the request and its telemetry."""
    observability_id = normalize_observability_id(value)
    http_request.state.observability_id = observability_id
    if observability_id is not None:
        # Telemetry is non-fatal.
        with contextlib.suppress(Exception):
            runtime.set_context(observability_id=observability_id)
    return observability_id


def observability_headers(observability_id: str | None) -> dict[str, str] | None:
    """Return the observability id response header, or None without an id."""
    normalized = normalize_observability_id(observability_id)
    if normalized is None:
        return None
    return {OBSERVABILITY_ID_HEADER: normalized}
