from contextlib import asynccontextmanager
from fastapi import FastAPI
from policyengine_fastapi.exit import exit
from policyengine_api_simulation import initialize
from policyengine_api_simulation.observability import (
    init_simulation_observability,
)
from policyengine_fastapi import ping
from policyengine_fastapi.health import (
    HealthRegistry,
    HealthSystemReporter,
)
import logging

"""
specific example instantiation of the app configured by a .env file
* in all environments we use sqlite
* observability emits standard structured logs through policyengine-observability;
  legacy Logfire export remains while we evaluate a replacement platform.
"""

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with exit.lifespan():
        yield


app = FastAPI(
    lifespan=lifespan,
    title="policyengine-api-simulation",
    summary="Policyengine simulation api",
)
init_simulation_observability(app, service_role="api")

# attach the api defined in the app package
initialize(app=app)

# attach ping routes
health_registry = HealthRegistry()
health_registry.register(HealthSystemReporter("general", {}))
ping.include_all_routers(app, health_registry)
