"""Independent Stage 12 simulation execution and report coordination."""

from .aggregation import (
    RetainedSimulationOutput,
    build_aggregate_report,
    build_spm_result,
)
from .coordination import (
    CoordinatorDependencies,
    CoordinatorInvocation,
    coordinate_report,
)
from .failures import (
    Stage12CancellationError,
    Stage12ExecutionError,
    Stage12FailureDetail,
    Stage12InputError,
    redacted_failure_detail,
    validate_policy_periods,
)
from .fanout import Stage12SegmentRunner, run_segmented_simulation
from .partition import (
    should_segment_simulation,
    stage12_region_groups,
    stage12_region_groups_for_model,
)
from .segment_contracts import Stage12SegmentInput, Stage12SegmentResult
from .segmentation import (
    build_segment_inputs,
    calculate_segment,
    merge_segment_results,
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
    "CoordinatorDependencies",
    "CoordinatorInvocation",
    "RetainedSimulationOutput",
    "SimulationCalculation",
    "Stage12CancellationError",
    "Stage12ExecutionError",
    "Stage12FailureDetail",
    "Stage12InputError",
    "Stage12SegmentInput",
    "Stage12SegmentResult",
    "Stage12SegmentRunner",
    "build_aggregate_report",
    "build_segment_inputs",
    "build_stage12_simulation",
    "calculate_segment",
    "calculate_simulation_frames",
    "coordinate_report",
    "merge_segment_results",
    "redacted_failure_detail",
    "run_segmented_simulation",
    "run_single_simulation",
    "should_segment_simulation",
    "simulation_input_sha256",
    "stage12_region_groups",
    "stage12_region_groups_for_model",
    "validate_policy_periods",
]
