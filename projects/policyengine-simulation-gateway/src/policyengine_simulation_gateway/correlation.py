"""Canonical HTTP transport for the diagnostic correlation identifier."""

from __future__ import annotations

from fastapi import FastAPI, Request
from policyengine_simulation_observability.identifiers import (
    OBSERVABILITY_ID_HEADER,
    normalize_observability_id,
)


def install_observability_id_middleware(app: FastAPI) -> None:
    """Store an incoming identifier candidate without creating a workflow."""

    @app.middleware("http")
    async def observability_id_transport(request: Request, call_next):
        request.state.incoming_observability_id = normalize_observability_id(
            request.headers.get(OBSERVABILITY_ID_HEADER)
        )
        request.state.observability_id = None
        response = await call_next(request)
        bound_observability_id = normalize_observability_id(
            request.state.observability_id
        )
        if bound_observability_id is not None:
            response.headers[OBSERVABILITY_ID_HEADER] = bound_observability_id
        elif OBSERVABILITY_ID_HEADER in response.headers:
            del response.headers[OBSERVABILITY_ID_HEADER]
        return response
