from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from policyengine_fastapi.health import HealthRegistry, HealthStatus


class PingRequest(BaseModel):
    value: int


class PingResponse(BaseModel):
    incremented: int


def create_router(health_registry: HealthRegistry) -> APIRouter:
    router = APIRouter()

    @router.post("/ping")
    async def ping(request: PingRequest) -> PingResponse:
        """
        Verify the api is able to recieve and process unauthenticated requests.
        """
        return PingResponse(incremented=request.value + 1)

    @router.get("/ping/started", response_model=str)
    async def started() -> str:
        """
        Verify the api is running.
        """
        return "alive"

    @router.get("/ping/alive", response_model=HealthStatus)
    async def alive() -> JSONResponse:
        """
        Check if the service is healthy. This will always return
        data, but will return a 503 response code if the service is
        unhealthy.
        """
        content = health_registry.report()
        return JSONResponse(
            content=content.model_dump(exclude_none=True),
            status_code=200 if content.healthy else 503,
        )

    return router


def include_all_routers(api: FastAPI, health_registry: HealthRegistry) -> None:
    api.include_router(create_router(health_registry))
