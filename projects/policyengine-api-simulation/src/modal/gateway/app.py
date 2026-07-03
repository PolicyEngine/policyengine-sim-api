"""
PolicyEngine Simulation Gateway - Modal App Definition

A lightweight, stable gateway that routes simulation requests to versioned
simulation apps. This app rarely changes and provides a stable URL for consumers.

The gateway looks up the appropriate versioned app from the version dicts
and spawns jobs on those apps.
"""

import modal

from src.modal.logfire_legacy import configure_logfire

# Stable app name - this should rarely change
app = modal.App("policyengine-simulation-gateway")
gateway_auth_secret = modal.Secret.from_name("policyengine-gateway-auth")
logfire_secret = modal.Secret.from_name("policyengine-logfire")

# Lightweight image for gateway - no heavy dependencies
gateway_image = (
    modal.Image.debian_slim(python_version="3.13")
    .pip_install(
        "fastapi>=0.115.0",
        "pydantic>=2.0",
        # PyJWT powers the bearer-token decoder in gateway.auth.
        "pyjwt>=2.10.1,<3.0.0",
        # JWTDecoder lives in the policyengine-fastapi lib; it only needs
        # the auth module at runtime here.
        "cryptography>=41.0.0",
        "logfire>=3.0.0",
        # logfire imports importlib_metadata unconditionally but does not
        # declare it as a dependency on Python 3.13, so install it
        # explicitly or the container crashes at startup.
        "importlib-metadata>=8",
        "policyengine-observability[fastapi]>=1.3.0,<2",
    )
    .add_local_python_source(
        "src.modal",
        "policyengine_api_simulation",
        copy=True,
    )
    .add_local_python_source("policyengine_fastapi", copy=True)
)


@app.function(image=gateway_image, secrets=[gateway_auth_secret, logfire_secret])
@modal.asgi_app()
def web_app():
    """
    FastAPI gateway for simulation job submission and polling.

    Provides stable endpoints:
      POST /simulate/economy/comparison - Submit a simulation job
      GET /jobs/{job_id} - Poll for job status
      GET /versions - List available versions
      GET /health - Health check
    """
    from fastapi import FastAPI

    from policyengine_api_simulation.observability import (
        configure_process_observability,
        init_simulation_observability,
    )
    from src.modal.gateway.auth import (
        enforce_auth_configured_guard,
        enforce_production_auth_guard,
    )
    from src.modal.gateway.endpoints import router

    api = FastAPI(
        title="PolicyEngine Simulation Gateway",
        description="Submit and poll simulation jobs. Routes to versioned simulation apps.",
        version="1.0.0",
    )
    configure_process_observability(
        platform="modal",
        service_role="modal_gateway",
        modal_app_name="policyengine-simulation-gateway",
        modal_function_name="web_app",
    )
    init_simulation_observability(api, service_role="modal_gateway")
    configure_logfire("policyengine-simulation-gateway")

    # Startup guard: crash the container if GATEWAY_AUTH_DISABLED is set in
    # a production-equivalent Modal environment, or set without the
    # explicit acknowledgement env var. This prevents the bypass from
    # accidentally shipping to prod if a dev deploy grabs the wrong secret
    # bundle. See gateway.auth.enforce_production_auth_guard for the rules.
    enforce_production_auth_guard()
    enforce_auth_configured_guard()

    api.include_router(router)
    return api
