"""Unauthenticated ``/ping`` liveness and health-probe routes."""

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from policyengine_fastapi.health import HealthRegistry, HealthStatus


class PingRequest(BaseModel):
    """Ping payload: an integer for the service to increment."""

    value: int


class PingResponse(BaseModel):
    """Ping reply carrying the request's ``value`` plus one."""

    incremented: int


def create_router(health_registry: HealthRegistry) -> APIRouter:
    """Build the ping routes; ``/ping/alive`` is 503 while the registry is unhealthy."""
    router = APIRouter()

    @router.post("/ping")
    async def ping(request: PingRequest) -> PingResponse:
        """Verify the API can receive and process unauthenticated requests."""
        return PingResponse(incremented=request.value + 1)

    @router.get("/ping/started")
    async def started() -> str:
        """Verify the api is running."""
        return "alive"

    @router.get("/ping/alive", response_model=HealthStatus)
    async def alive() -> JSONResponse:
        """Report whether the service is healthy.

        The health report is always returned, with status 200 when the service
        is healthy and 503 when it is not.
        """
        content = health_registry.report()
        return JSONResponse(
            content=content.model_dump(exclude_none=True),
            status_code=200 if content.healthy else 503,
        )

    return router


def include_all_routers(api: FastAPI, health_registry: HealthRegistry) -> None:
    """Mount the ping routes on ``api``, reporting health from ``health_registry``."""
    api.include_router(create_router(health_registry))
