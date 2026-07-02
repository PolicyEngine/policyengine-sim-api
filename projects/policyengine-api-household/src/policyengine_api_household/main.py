from fastapi import FastAPI

from policyengine_api_household import initialize
from policyengine_fastapi import ping
from policyengine_fastapi.health import (
    HealthRegistry,
    HealthSystemReporter,
)

"""
Local-run experiment entrypoint for the household calculation service.

Deliberately minimal: no Modal, no auth, no OpenTelemetry/deploy wiring —
just the calculate router plus the repo's standard ping/health routes
(POST /ping, GET /ping/started, GET /ping/alive).
"""

app = FastAPI(
    title="policyengine-api-household",
    summary="PolicyEngine household calculation API (v1-parity experiment)",
)

# attach the api defined in the app package
initialize(app=app)

# attach ping routes
health_registry = HealthRegistry()
health_registry.register(HealthSystemReporter("general", {}))
ping.include_all_routers(app, health_registry)
