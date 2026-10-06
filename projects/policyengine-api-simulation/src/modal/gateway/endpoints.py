"""
FastAPI endpoints for the Gateway API.
"""

import logging
from typing import Optional

import modal
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src.modal.gateway.models import (
    JobStatusResponse,
    JobSubmitResponse,
    PingRequest,
    PingResponse,
    PolicyEngineBundle,
    SimulationRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()
JOB_METADATA_DICT_NAME = "simulation-api-job-metadata"


def _job_metadata_store():
    return modal.Dict.from_name(JOB_METADATA_DICT_NAME, create_if_missing=True)


def _build_policyengine_bundle(
    resolved_version: str, payload: dict
) -> PolicyEngineBundle:
    return PolicyEngineBundle(
        model_version=resolved_version,
        dataset=payload.get("data"),
    )


def _serialize_job_metadata(
    resolved_app_name: str, bundle: PolicyEngineBundle
) -> dict:
    return {
        "resolved_app_name": resolved_app_name,
        "policyengine_bundle": bundle.model_dump(),
    }


def get_app_name(country: str, version: Optional[str]) -> tuple[str, str]:
    """
    Resolve country + version to Modal app name.

    Returns:
        Tuple of (app_name, resolved_version)
    """
    country_lower = country.lower()
    if country_lower not in ("us", "uk"):
        raise ValueError(f"Unknown country: {country}")

    version_dict = modal.Dict.from_name(f"simulation-api-{country_lower}-versions")

    # Resolve version
    if version is None:
        resolved_version = version_dict["latest"]
    else:
        resolved_version = version

    # Get app name for this version
    try:
        app_name = version_dict[resolved_version]
    except KeyError:
        raise ValueError(f"Unknown version {resolved_version} for country {country}")

    return app_name, resolved_version


@router.post("/simulate/economy/comparison", response_model=JobSubmitResponse)
async def submit_simulation(request: SimulationRequest):
    """
    Submit a simulation job.

    Matches the existing Cloud Run API endpoint path.
    Routes to the appropriate app based on country and version params.
    Returns immediately with job_id for polling.
    """
    try:
        app_name, resolved_version = get_app_name(request.country, request.version)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    payload = request.model_dump(exclude={"version"})

    logger.info(f"Routing {request.country}:{resolved_version} to app {app_name}")

    # Get function reference from the target app
    sim_func = modal.Function.from_name(app_name, "run_simulation")

    # Spawn the job (returns immediately)
    call = sim_func.spawn(payload)

    bundle = _build_policyengine_bundle(resolved_version, payload)
    job_metadata = _serialize_job_metadata(app_name, bundle)
    _job_metadata_store()[call.object_id] = job_metadata

    return JobSubmitResponse(
        job_id=call.object_id,
        status="submitted",
        poll_url=f"/jobs/{call.object_id}",
        country=request.country,
        version=resolved_version,
        resolved_app_name=app_name,
        policyengine_bundle=bundle,
    )


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str):
    """
    Poll for job status.

    Returns:
        - 200 with status="complete" and result when done
        - 202 with status="running" while in progress
        - 500 with status="failed" and error on failure
        - 404 if job_id not found
    """
    try:
        call = modal.FunctionCall.from_id(job_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    job_metadata = _job_metadata_store().get(job_id)

    try:
        result = call.get(timeout=0)
        return JobStatusResponse(status="complete", result=result, **(job_metadata or {}))
    except TimeoutError:
        return JSONResponse(
            status_code=202,
            content={
                "status": "running",
                "result": None,
                "error": None,
                **(job_metadata or {}),
            },
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={
                "status": "failed",
                "result": None,
                "error": str(e),
                **(job_metadata or {}),
            },
        )


@router.get("/versions")
async def list_versions():
    """List all available versions for all countries."""
    us_dict = modal.Dict.from_name("simulation-api-us-versions")
    uk_dict = modal.Dict.from_name("simulation-api-uk-versions")

    return {
        "us": dict(us_dict),
        "uk": dict(uk_dict),
    }


@router.get("/versions/{country}")
async def get_country_versions(country: str):
    """Get available versions for a specific country."""
    country_lower = country.lower()
    if country_lower not in ("us", "uk"):
        raise HTTPException(status_code=404, detail=f"Unknown country: {country}")

    version_dict = modal.Dict.from_name(f"simulation-api-{country_lower}-versions")
    return dict(version_dict)


@router.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@router.post("/ping", response_model=PingResponse)
async def ping(request: PingRequest) -> PingResponse:
    """
    Verify the API is able to receive and process requests.
    Matches the policyengine_fastapi.ping endpoint for test compatibility.
    """
    return PingResponse(incremented=request.value + 1)
