"""Independent Stage 12 simulation execution and report coordination."""

from .aggregation import build_aggregate_report, build_spm_result
from .coordination import coordinate_report
from .simulation import (
    SimulationCalculation,
    calculate_simulation_frames,
    run_single_simulation,
    simulation_input_sha256,
)

# Retain the existing internal import used by the focused runtime tests while
# locating the implementation with the rest of the aggregation logic.
_build_spm_result = build_spm_result

__all__ = [
    "SimulationCalculation",
    "build_aggregate_report",
    "calculate_simulation_frames",
    "coordinate_report",
    "run_single_simulation",
    "simulation_input_sha256",
]
