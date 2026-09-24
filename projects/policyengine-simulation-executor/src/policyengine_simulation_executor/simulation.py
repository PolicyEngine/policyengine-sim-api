import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from policyengine_observability import ObservabilityRuntime
from policyengine_simulation_contract.spm import spm_error_detail

from policyengine_simulation_executor.simulation_runtime import run_simulation_impl
from policyengine_simulation_executor.compat_models import (
    EconomyComparison,
    SimulationOptions,
)

logger = logging.getLogger(__file__)


def create_router(runtime: ObservabilityRuntime):
    router = APIRouter()

    @router.post("/simulate/economy/comparison", response_model=EconomyComparison)
    async def simulate(
        parameters: SimulationOptions,
    ) -> EconomyComparison | JSONResponse:
        logger.info("Calculating comparison")
        try:
            payload = parameters.model_dump(mode="json", exclude_none=True)
            if parameters.spm is not None:
                payload["spm"] = parameters.spm.model_dump(mode="json")
            result = run_simulation_impl(payload, runtime=runtime)
        except ValueError as exc:
            detail = spm_error_detail(exc)
            if detail:
                return JSONResponse(
                    status_code=400,
                    content={
                        "status": "failed",
                        "result": None,
                        "error": detail.message,
                        "errors": [detail.model_dump()],
                    },
                )
            raise
        logger.info("Comparison complete")
        return EconomyComparison.model_validate(result)

    return router
