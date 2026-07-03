"""Cliff impact output segment builders."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from policyengine_simulation_executor.simulation_macro_output import (
    CliffImpactInSimulation,
    CliffImpactOutput,
)
from policyengine_simulation_executor.simulation_output_common import _output_model_dump


def build_cliff_impact(analysis: Any) -> CliffImpactOutput | None:
    cliff_impact = getattr(analysis, "cliff_impact", None)
    if isinstance(cliff_impact, CliffImpactOutput):
        return cliff_impact
    output = _output_model_dump(cliff_impact)
    if not isinstance(output, Mapping):
        return None
    return CliffImpactOutput(
        baseline=CliffImpactInSimulation(**output["baseline"]),
        reform=CliffImpactInSimulation(**output["reform"]),
    )
