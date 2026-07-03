"""
Generate OpenAPI spec for the Modal Gateway API.

This creates a FastAPI app with the same route signatures as the gateway
but without Modal dependencies, allowing OpenAPI generation without credentials.

Usage:
    cd projects/policyengine-simulation-executor
    uv run python -m policyengine_simulation_gateway.generate_openapi
"""

import json
from pathlib import Path

from fastapi import FastAPI

from policyengine_simulation_contract.gateway_models import (
    BudgetWindowBatchRequest,
    BudgetWindowBatchStatusResponse,
    BudgetWindowBatchSubmitResponse,
    JobStatusResponse,
    JobSubmitResponse,
    PingRequest,
    PingResponse,
    SimulationRequest,
)


def create_openapi_app() -> FastAPI:
    """Create FastAPI app for OpenAPI generation."""
    app = FastAPI(
        title="PolicyEngine Simulation Gateway API",
        description="Submit and poll simulation jobs on Modal",
        version="1.0.0",
    )

    @app.post(
        "/simulate/economy/comparison",
        response_model=JobSubmitResponse,
        responses={
            200: {"description": "Job submitted successfully"},
            400: {"description": "Invalid request (unknown country/version)"},
        },
    )
    async def submit_simulation(request: SimulationRequest) -> JobSubmitResponse:
        """
        Submit a simulation job.

        Routes to the appropriate simulation app based on country and version.
        Returns immediately with a job_id for polling.
        """
        raise NotImplementedError("Stub for OpenAPI generation")

    @app.post(
        "/simulate/economy/budget-window",
        response_model=BudgetWindowBatchSubmitResponse,
        responses={
            200: {"description": "Budget-window batch submitted successfully"},
            400: {"description": "Invalid request (unknown country/version/year)"},
        },
    )
    async def submit_budget_window_batch(
        request: BudgetWindowBatchRequest,
    ) -> BudgetWindowBatchSubmitResponse:
        """
        Submit a budget-window batch job.

        Returns immediately with a parent batch job ID for polling.
        """
        raise NotImplementedError("Stub for OpenAPI generation")

    @app.get(
        "/jobs/{job_id}",
        response_model=JobStatusResponse,
        responses={
            200: {"description": "Job complete", "model": JobStatusResponse},
            202: {"description": "Job still running"},
            404: {"description": "Job not found"},
            500: {"description": "Job failed"},
        },
    )
    async def get_job_status(job_id: str) -> JobStatusResponse:
        """
        Poll for job status.

        Returns:
            - 200 with status="complete" and result when done
            - 202 with status="running" while in progress
            - 404 if job_id not found
            - 500 with status="failed" and error on failure
        """
        raise NotImplementedError("Stub for OpenAPI generation")

    @app.get(
        "/budget-window-jobs/{batch_job_id}",
        response_model=BudgetWindowBatchStatusResponse,
        responses={
            200: {
                "description": "Batch complete",
                "model": BudgetWindowBatchStatusResponse,
            },
            202: {"description": "Batch submitted or running"},
            404: {"description": "Batch job not found"},
            500: {"description": "Batch failed"},
        },
    )
    async def get_budget_window_job_status(
        batch_job_id: str,
    ) -> BudgetWindowBatchStatusResponse:
        """
        Poll for budget-window batch status.
        """
        raise NotImplementedError("Stub for OpenAPI generation")

    @app.get("/versions")
    async def list_versions() -> dict:
        """List all available routing versions."""
        raise NotImplementedError("Stub for OpenAPI generation")

    @app.get("/versions/{kind}")
    async def get_country_versions(kind: str) -> dict:
        """Get available versions for policyengine, US, or UK routing."""
        raise NotImplementedError("Stub for OpenAPI generation")

    @app.get("/health")
    async def health() -> dict:
        """Health check endpoint."""
        return {"status": "healthy"}

    @app.post("/ping", response_model=PingResponse)
    async def ping(request: PingRequest) -> PingResponse:
        """Verify the API is able to receive and process requests."""
        raise NotImplementedError("Stub for OpenAPI generation")

    return app


def main():
    """Generate OpenAPI spec and write to artifacts."""
    app = create_openapi_app()
    openapi_spec = app.openapi()

    output_path = Path(__file__).resolve().parents[2] / "artifacts" / "openapi.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(openapi_spec, f, indent=2)

    print(f"OpenAPI spec written to {output_path}")


if __name__ == "__main__":
    main()
