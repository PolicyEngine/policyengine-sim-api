"""
Pydantic models for the Gateway API.
"""

from typing import Optional
from pydantic import BaseModel, ConfigDict


class SimulationRequest(BaseModel):
    """Request model for simulation submission."""

    country: str
    version: Optional[str] = None

    model_config = ConfigDict(extra="allow")  # Pass through all other fields


class PolicyEngineBundle(BaseModel):
    """Resolved runtime provenance returned by the gateway."""

    model_version: str
    policyengine_version: Optional[str] = None
    data_version: Optional[str] = None
    dataset: Optional[str] = None


class JobSubmitResponse(BaseModel):
    """Response model for job submission."""

    job_id: str
    status: str
    poll_url: str
    country: str
    version: str
    resolved_app_name: str
    policyengine_bundle: PolicyEngineBundle


class JobStatusResponse(BaseModel):
    """Response model for job status polling."""

    status: str
    result: Optional[dict] = None
    error: Optional[str] = None
    resolved_app_name: Optional[str] = None
    policyengine_bundle: Optional[PolicyEngineBundle] = None


class PingRequest(BaseModel):
    """Request model for ping endpoint."""

    value: int


class PingResponse(BaseModel):
    """Response model for ping endpoint."""

    incremented: int
