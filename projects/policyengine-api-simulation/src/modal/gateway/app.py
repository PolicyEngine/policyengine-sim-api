"""
PolicyEngine Simulation Gateway - Modal App Definition

A lightweight, stable gateway that routes simulation requests to versioned
simulation apps. This app rarely changes and provides a stable URL for consumers.

The gateway looks up the appropriate versioned app from the version dicts
and spawns jobs on those apps.
"""

import modal

# Stable app name - this should rarely change
app = modal.App("policyengine-simulation-gateway")

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
    )
    .add_local_python_source("src.modal", copy=True)
    .add_local_python_source("policyengine_fastapi", copy=True)
)


@app.function(image=gateway_image)
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

    from src.modal.gateway.auth import enforce_production_auth_guard
    from src.modal.gateway.endpoints import router

    # Startup guard: crash the container if GATEWAY_AUTH_DISABLED is set in
    # a production-equivalent Modal environment, or set without the
    # explicit acknowledgement env var. This prevents the bypass from
    # accidentally shipping to prod if a dev deploy grabs the wrong secret
    # bundle. See gateway.auth.enforce_production_auth_guard for the rules.
    enforce_production_auth_guard()

    api = FastAPI(
        title="PolicyEngine Simulation Gateway",
        description="Submit and poll simulation jobs. Routes to versioned simulation apps.",
        version="1.0.0",
    )
    api.include_router(router)
    return api
