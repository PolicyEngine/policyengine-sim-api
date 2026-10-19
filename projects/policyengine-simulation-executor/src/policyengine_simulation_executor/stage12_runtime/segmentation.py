"""Independent Stage 12 segment calculation and exact national merge."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING

import pandas as pd
from policyengine_simulation_observability.stages import STAGE12_SEGMENT_STAGES

from policyengine_simulation_executor.stage12_artifacts import (
    deserialize_calculation_provenance,
    deserialize_simulation_frames,
    serialize_simulation_frames,
)

from .output_planning import validate_output_frames
from .partition import NATIONAL_PARTITION_GROUP_COUNT, stage12_region_groups
from .segment_contracts import Stage12SegmentInput, Stage12SegmentResult
from .simulation import SimulationCalculation, calculate_simulation_frames

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.spm import SPMProvenance
    from policyengine_simulation_contract.stage12_execution import (
        PlannedSimulationExecutionInput,
    )


def build_segment_inputs(
    simulation: PlannedSimulationExecutionInput,
    *,
    groups: Sequence[Sequence[str]] | None = None,
) -> tuple[Stage12SegmentInput, ...]:
    """Build one immutable single-policy input per region group."""
    selected = groups or stage12_region_groups(simulation.geography.country)
    if not selected:
        msg = "Stage 12 simulation has no national partition"
        raise ValueError(msg)
    if len(selected) != NATIONAL_PARTITION_GROUP_COUNT:
        msg = (
            "Stage 12 national partition must contain "
            f"{NATIONAL_PARTITION_GROUP_COUNT} groups"
        )
        raise ValueError(msg)
    return tuple(
        Stage12SegmentInput(
            simulation=simulation,
            segment_index=index,
            region_codes=tuple(group),
        )
        for index, group in enumerate(selected)
    )


def _scoped_simulation(segment: Stage12SegmentInput) -> PlannedSimulationExecutionInput:
    options = {
        key: value
        for key, value in segment.simulation.options.items()
        if key != "segmented"
    }
    options["region_group"] = list(segment.region_codes)
    return segment.simulation.model_copy(update={"options": options})


def calculate_segment(
    payload: object,
    *,
    calculator: Callable[
        [PlannedSimulationExecutionInput],
        SimulationCalculation | Mapping[str, pd.DataFrame],
    ] = calculate_simulation_frames,
    runtime: ObservabilityRuntime | None = None,
) -> Stage12SegmentResult:
    """Calculate one segment without persistence or report aggregation."""
    segment = Stage12SegmentInput.model_validate(payload)
    scoped = _scoped_simulation(segment)
    if calculator is calculate_simulation_frames:
        calculated = calculate_simulation_frames(
            scoped,
            runtime=runtime,
            stage_plan=STAGE12_SEGMENT_STAGES,
        )
    else:
        calculated = calculator(scoped)
    if isinstance(calculated, SimulationCalculation):
        frames = calculated.frames
        provenance = calculated.calculation_provenance
    else:
        frames = calculated
        provenance = None
    validate_output_frames(frames, scoped.output_plan)
    parquet_payload, _ = serialize_simulation_frames(
        frames,
        calculation_provenance=provenance,
    )
    return Stage12SegmentResult(
        segment_index=segment.segment_index,
        role=segment.simulation.role,
        region_codes=segment.region_codes,
        parquet_payload=parquet_payload,
        payload_sha256=sha256(parquet_payload).hexdigest(),
    )


def _merge_calculation_provenance(
    values: Sequence[SPMProvenance | None],
) -> SPMProvenance | None:
    """Keep one shared compact receipt only when every segment agrees."""
    present = [value for value in values if value is not None]
    if not present:
        return None
    if len(present) != len(values):
        msg = "segment calculation metadata is incomplete"
        raise ValueError(msg)
    receipt = present[0]
    if any(value != receipt for value in present[1:]):
        msg = "segment calculation provenance does not match"
        raise ValueError(msg)
    return receipt


def merge_segment_results(
    simulation: PlannedSimulationExecutionInput,
    payloads: Sequence[object],
    *,
    groups: Sequence[Sequence[str]] | None = None,
) -> SimulationCalculation:
    """Validate and merge all segment payloads into one national result."""
    expected = build_segment_inputs(simulation, groups=groups)
    results = [Stage12SegmentResult.model_validate(payload) for payload in payloads]
    if len(results) != len(expected):
        msg = "Stage 12 segmented calculation returned an incomplete result set"
        raise ValueError(msg)
    by_index: dict[int, Stage12SegmentResult] = {}
    for result in results:
        if result.segment_index in by_index:
            msg = "Stage 12 segmented calculation returned a duplicate index"
            raise ValueError(msg)
        by_index[result.segment_index] = result

    decoded: list[dict[str, pd.DataFrame]] = []
    provenance: list[SPMProvenance | None] = []
    for segment in expected:
        result = by_index.get(segment.segment_index)
        if result is None:
            msg = "Stage 12 segmented calculation omitted a segment"
            raise ValueError(msg)
        if result.role is not simulation.role:
            msg = "Stage 12 segment result names another simulation role"
            raise ValueError(msg)
        if result.region_codes != segment.region_codes:
            msg = "Stage 12 segment result names another region group"
            raise ValueError(msg)
        decoded.append(deserialize_simulation_frames(result.parquet_payload))
        provenance.append(deserialize_calculation_provenance(result.parquet_payload))

    expected_entities = set(decoded[0])
    if any(set(frames) != expected_entities for frames in decoded[1:]):
        msg = "Stage 12 segment entity schemas do not match"
        raise ValueError(msg)
    merged: dict[str, pd.DataFrame] = {}
    for entity in sorted(expected_entities):
        parts = [frames[entity] for frames in decoded]
        first_dtypes = {column: str(dtype) for column, dtype in parts[0].dtypes.items()}
        for part in parts[1:]:
            if {
                column: str(dtype) for column, dtype in part.dtypes.items()
            } != first_dtypes:
                msg = "Stage 12 segment column dtypes do not match"
                raise ValueError(msg)
        frame = pd.concat(parts, ignore_index=True)
        identifier = f"{entity}_id"
        if identifier not in frame:
            msg = f"Stage 12 segment entity {entity!r} has no identifier"
            raise ValueError(msg)
        if frame[identifier].duplicated().any():
            msg = f"Stage 12 segment entity {entity!r} has duplicate IDs"
            raise ValueError(msg)
        merged[entity] = frame
    validate_output_frames(merged, simulation.output_plan)
    return SimulationCalculation(
        frames=merged,
        calculation_provenance=_merge_calculation_provenance(provenance),
        cache_outcome=None,
    )
