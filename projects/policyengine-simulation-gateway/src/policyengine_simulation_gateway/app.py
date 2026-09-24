"""
PolicyEngine Simulation Gateway - Modal App Definition

A lightweight, stable gateway that routes simulation requests to versioned
simulation apps. This app rarely changes and provides a stable URL for consumers.

The gateway looks up the appropriate versioned app from the version dicts
and spawns jobs on those apps.
"""

import modal
import os
from pathlib import Path

from policyengine_simulation_observability.observability import (
    modal_image_environment,
)

# Stable app name - this should rarely change
app = modal.App("policyengine-simulation-gateway")
gateway_auth_secret = modal.Secret.from_name("policyengine-gateway-auth")
OBSERVABILITY_ENV = modal_image_environment()

# Lightweight image for gateway - no heavy dependencies.
#
# uv_sync installs this project's [project.dependencies] straight from
# uv.lock (frozen), so the image environment is exactly what CI unit
# tests run against — packages can only change through a relock.
# --no-default-groups is load-bearing: uv sync installs the dev group by
# default, and that group holds ../../libs path dependencies that do not
# exist in Modal's build context (uv_sync ships only pyproject + lock).
# Local packages arrive as mounted source below instead.
# The project dir only resolves locally: at deploy time the image
# definition is built on the developer/CI machine. Inside the container
# this module mounts at /root/app.py (no parents[2]), and the image
# definition is never used there — so short-circuit the path.
_UV_PROJECT_DIR = str(Path(__file__).resolve().parents[2]) if modal.is_local() else "."


def build_gateway_image() -> modal.Image:
    """The gateway image, shared with the smoke app (smoke_app.py) so
    both construct identical layer definitions and share Modal's
    content-addressed cache."""
    return (
        modal.Image.debian_slim(python_version="3.13")
        .uv_sync(
            uv_project_dir=_UV_PROJECT_DIR,
            frozen=True,
            extra_options="--no-default-groups",
        )
        .add_local_python_source(
            "policyengine_simulation_gateway",
            "policyengine_simulation_contract",
            "policyengine_simulation_observability",
            "policyengine_fastapi",
            copy=True,
        )
        .env(OBSERVABILITY_ENV)
    )


gateway_image = build_gateway_image()


@app.function(image=gateway_image, secrets=[gateway_auth_secret])
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

    from policyengine_simulation_observability.observability import (
        init_simulation_observability,
    )
    from policyengine_simulation_gateway.auth import (
        enforce_auth_configured_guard,
        enforce_production_auth_guard,
    )
    from policyengine_simulation_gateway.endpoints import router

    api = FastAPI(
        title="PolicyEngine Simulation Gateway",
        description="Submit and poll simulation jobs. Routes to versioned simulation apps.",
        version="1.0.0",
    )
    runtime = init_simulation_observability(
        api,
        service_name="policyengine-simulation-gateway",
        service_role="modal_gateway",
        platform="modal",
        environment=os.getenv("MODAL_ENVIRONMENT", "local"),
    )
    api.add_event_handler("shutdown", runtime.shutdown)

    # Startup guard: crash the container if GATEWAY_AUTH_DISABLED is set in
    # a production-equivalent Modal environment, or set without the
    # explicit acknowledgement env var. This prevents the bypass from
    # accidentally shipping to prod if a dev deploy grabs the wrong secret
    # bundle. See gateway.auth.enforce_production_auth_guard for the rules.
    enforce_production_auth_guard(runtime)
    enforce_auth_configured_guard()

    api.include_router(router)
    return api
