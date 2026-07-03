"""Labor supply response output segment builders."""

from __future__ import annotations

from typing import Any

from policyengine_simulation_executor.simulation_macro_output import (
    LaborSupplyResponseOutput,
)
from policyengine_simulation_executor.simulation_output_common import _output_model_dump


def build_labor_supply_response(analysis: Any) -> LaborSupplyResponseOutput | None:
    labor_supply_response = getattr(analysis, "labor_supply_response", None)
    if isinstance(labor_supply_response, LaborSupplyResponseOutput):
        return labor_supply_response
    output = _output_model_dump(labor_supply_response)
    return LaborSupplyResponseOutput(output) if isinstance(output, dict) else None
