from contextlib import asynccontextmanager
from fastapi import FastAPI
from policyengine_fastapi.exit import exit
from policyengine_simulation_executor import initialize
from policyengine_simulation_observability.observability import (
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
* observability emits structured logs, traces, and metrics through
  policyengine-observability.
"""

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with exit.lifespan():
        try:
            yield
        finally:
            runtime.shutdown()


app = FastAPI(
    lifespan=lifespan,
    title="policyengine-simulation-executor",
    summary="Policyengine simulation api",
)
runtime = init_simulation_observability(
    app,
    service_name="policyengine-simulation-executor",
    service_role="api",
    platform="other",
    environment="local",
)

# attach the api defined in the app package
initialize(app=app, runtime=runtime)

# attach ping routes
health_registry = HealthRegistry()
health_registry.register(HealthSystemReporter("general", {}))
ping.include_all_routers(app, health_registry)
