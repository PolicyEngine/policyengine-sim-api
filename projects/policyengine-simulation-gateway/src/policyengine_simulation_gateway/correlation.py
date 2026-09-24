"""Canonical HTTP transport for the diagnostic correlation identifier."""

from __future__ import annotations

from fastapi import FastAPI, Request
from policyengine_simulation_observability.identifiers import (
    OBSERVABILITY_ID_HEADER,
    normalize_observability_id,
    resolve_observability_id,
)
from starlette.datastructures import MutableHeaders


def install_observability_id_middleware(app: FastAPI) -> None:
    """Resolve one request identifier and return it on every response."""

    @app.middleware("http")
    async def observability_id_transport(request: Request, call_next):
        observability_id = resolve_observability_id(
            request.headers.get(OBSERVABILITY_ID_HEADER)
        )
        MutableHeaders(scope=request.scope)[OBSERVABILITY_ID_HEADER] = observability_id
        request.state.observability_id = observability_id
        response = await call_next(request)
        response.headers[OBSERVABILITY_ID_HEADER] = (
            normalize_observability_id(response.headers.get(OBSERVABILITY_ID_HEADER))
            or observability_id
        )
        return response
