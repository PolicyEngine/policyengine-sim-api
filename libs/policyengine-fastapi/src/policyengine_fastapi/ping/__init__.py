"""Unauthenticated ``/ping`` liveness and health-probe routes."""

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from policyengine_fastapi.health import HealthRegistry, HealthStatus


class PingRequest(BaseModel):  # noqa: D101  # docstring would change the published OpenAPI description
    value: int


class PingResponse(BaseModel):  # noqa: D101  # docstring would change the published OpenAPI description
    incremented: int


def create_router(health_registry: HealthRegistry) -> APIRouter:
    """Build the ping routes; ``/ping/alive`` is 503 while the registry is unhealthy."""
    router = APIRouter()

    @router.post("/ping")
    async def ping(request: PingRequest) -> PingResponse:
        """Verify the api is able to recieve and process unauthenticated requests."""
        return PingResponse(incremented=request.value + 1)

    @router.get("/ping/started", response_model=str)
    async def started() -> str:
        """Verify the api is running."""
        return "alive"

    @router.get("/ping/alive", response_model=HealthStatus)
    async def alive() -> JSONResponse:
        """Check if the service is healthy. This will always return
        data, but will return a 503 response code if the service is
        unhealthy.
        """  # noqa: D205  # docstring would change the published OpenAPI description
        content = health_registry.report()
        return JSONResponse(
            content=content.model_dump(exclude_none=True),
            status_code=200 if content.healthy else 503,
        )

    return router


def include_all_routers(api: FastAPI, health_registry: HealthRegistry) -> None:
    """Mount the ping routes on ``api``, reporting health from ``health_registry``."""
    api.include_router(create_router(health_registry))
