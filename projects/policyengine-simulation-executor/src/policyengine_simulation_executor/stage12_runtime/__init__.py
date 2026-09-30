"""Independent Stage 12 simulation execution and report coordination."""

from .aggregation import build_aggregate_report, build_spm_result
from .coordination import coordinate_report
from .failures import (
    Stage12ExecutionError,
    Stage12FailureDetail,
    Stage12InputError,
    failure_detail_from_exception,
    validate_policy_periods,
)
from .fanout import Stage12SegmentRunner, run_segmented_simulation
from .partition import stage12_region_groups, stage12_region_groups_for_model
from .segment_contracts import Stage12SegmentInput, Stage12SegmentResult
from .segmentation import (
    build_segment_inputs,
    calculate_segment,
    merge_segment_results,
    should_segment_simulation,
)
from .simulation import (
    SimulationCalculation,
    build_stage12_simulation,
    calculate_simulation_frames,
    run_single_simulation,
    simulation_input_sha256,
)

# Retain the existing internal import used by the focused runtime tests while
# locating the implementation with the rest of the aggregation logic.
_build_spm_result = build_spm_result

__all__ = [
    "SimulationCalculation",
    "Stage12SegmentInput",
    "Stage12SegmentResult",
    "Stage12SegmentRunner",
    "Stage12ExecutionError",
    "Stage12FailureDetail",
    "Stage12InputError",
    "build_aggregate_report",
    "build_segment_inputs",
    "build_stage12_simulation",
    "calculate_simulation_frames",
    "calculate_segment",
    "coordinate_report",
    "failure_detail_from_exception",
    "run_single_simulation",
    "run_segmented_simulation",
    "merge_segment_results",
    "simulation_input_sha256",
    "stage12_region_groups",
    "stage12_region_groups_for_model",
    "should_segment_simulation",
    "validate_policy_periods",
]
