"""Unit tests for Stage 12 segment calculation and merging."""

from __future__ import annotations

from hashlib import sha256

import pandas as pd
import pytest
from policyengine_simulation_contract.spm import (
    SPMProvenance,
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_provenance,
)
from policyengine_simulation_contract.stage12_execution import (
    PlannedSimulationExecutionInput,
    SimulationRole,
)
from policyengine_simulation_executor.stage12_artifacts import (
    serialize_simulation_frames,
)
from policyengine_simulation_executor.stage12_runtime import (
    SimulationCalculation,
    Stage12SegmentResult,
    build_segment_inputs,
    calculate_segment,
    merge_segment_results,
    should_segment_simulation,
)
from test_stage12_runtime import _planned_simulation


def _frames(index: int) -> dict[str, pd.DataFrame]:
    return {
        "household": pd.DataFrame(
            {
                "household_id": pd.Series([index], dtype="int64"),
                "household_net_income": pd.Series([index + 0.5], dtype="float32"),
            }
        ),
        "person": pd.DataFrame(
            {
                "age": pd.Series([30 + index], dtype="int16"),
                "household_id": pd.Series([index], dtype="int64"),
                "person_id": pd.Series([100 + index], dtype="int64"),
            }
        ),
    }


def _results(
    simulation: PlannedSimulationExecutionInput, provenance: SPMProvenance | None = None
) -> list[Stage12SegmentResult]:
    results = []
    for segment in build_segment_inputs(simulation):
        payload, _ = serialize_simulation_frames(
            _frames(segment.segment_index), calculation_provenance=provenance
        )
        results.append(
            Stage12SegmentResult(
                segment_index=segment.segment_index,
                role=simulation.role,
                region_codes=segment.region_codes,
                parquet_payload=payload,
                payload_sha256=sha256(payload).hexdigest(),
            )
        )
    return results


def _spm_provenance() -> SPMProvenance:
    """Build an explicit test fixture, not installed runtime version metadata."""
    selection = SPMSelection(
        forecast_content_sha256="f" * 64,
        scenario="official",
        geography_kind="national",
        geography_id=None,
        county_vintage="2020",
        as_of=None,
    )
    return build_spm_provenance(
        forecast_id="test-forecast",
        forecast_sha256="f" * 64,
        selection=selection,
        years=("2026",),
        runtime_versions=SPMRuntimeVersions(
            policyengine="test-fixture",
            policyengine_core="test-fixture",
            policyengine_us="test-fixture",
            spm_calculator="test-fixture",
        ),
    )


def test_merge_preserves_one_identical_compact_spm_receipt() -> None:
    simulation = _planned_simulation(SimulationRole.BASELINE)
    receipt = _spm_provenance()

    calculated = merge_segment_results(simulation, _results(simulation, receipt))

    assert calculated.calculation_provenance == receipt
    assert isinstance(calculated.calculation_provenance, SPMProvenance)


@pytest.mark.parametrize("difference", ["missing", "scenario", "versions", "years"])
def test_merge_rejects_missing_or_incompatible_segment_spm_receipts(
    difference: str,
) -> None:
    simulation = _planned_simulation(SimulationRole.BASELINE)
    receipt = _spm_provenance()
    results = _results(simulation, receipt)
    alternative = {
        "missing": None,
        "scenario": receipt.model_copy(update={"scenario": "alternative"}),
        "versions": receipt.model_copy(
            update={
                "runtime_versions": receipt.runtime_versions.model_copy(
                    update={"policyengine": "different-test-fixture"}
                )
            }
        ),
        "years": receipt.model_copy(update={"years": ("2027",)}),
    }[difference]
    payload, _ = serialize_simulation_frames(
        _frames(0), calculation_provenance=alternative
    )
    results[0] = results[0].model_copy(
        update={
            "parquet_payload": payload,
            "payload_sha256": sha256(payload).hexdigest(),
        }
    )

    with pytest.raises(ValueError, match="segment calculation"):
        merge_segment_results(simulation, results)


def test_eligibility_matches_stage12_supported_national_shape() -> None:
    simulation = _planned_simulation(SimulationRole.BASELINE)
    requirements = simulation.output_plan.requirements
    assert should_segment_simulation(simulation) is True
    assert (
        should_segment_simulation(
            simulation.model_copy(update={"options": {"segmented": False}})
        )
        is False
    )
    assert (
        should_segment_simulation(
            simulation.model_copy(
                update={
                    "geography": simulation.geography.model_copy(
                        update={"region": "state/ca"}
                    )
                }
            )
        )
        is False
    )
    assert (
        should_segment_simulation(
            simulation.model_copy(
                update={
                    "output_plan": simulation.output_plan.model_copy(
                        update={
                            "requirements": requirements.model_copy(
                                update={"include_cliff_impacts": True}
                            )
                        }
                    )
                }
            )
        )
        is False
    )


def test_builds_twenty_single_policy_segment_inputs() -> None:
    simulation = _planned_simulation(SimulationRole.REFORM)

    segments = build_segment_inputs(simulation)

    assert len(segments) == 20
    assert {segment.segment_index for segment in segments} == set(range(20))
    assert all(segment.simulation.role is SimulationRole.REFORM for segment in segments)
    assert all(segment.simulation.policy == simulation.policy for segment in segments)


def test_segment_calculation_scopes_one_group_and_preserves_dtypes() -> None:
    simulation = _planned_simulation(SimulationRole.BASELINE)
    segment = build_segment_inputs(simulation)[0]
    received = []

    def calculator(scoped):
        received.append(scoped)
        return SimulationCalculation(frames=_frames(0))

    result = calculate_segment(segment, calculator=calculator)
    merged = merge_segment_results(
        simulation,
        [
            calculate_segment(
                item,
                calculator=lambda scoped, index=item.segment_index: (
                    SimulationCalculation(frames=_frames(index))
                ),
            )
            for item in build_segment_inputs(simulation)
        ],
    )

    assert received[0].options["region_group"] == list(segment.region_codes)
    assert "segmented" not in received[0].options
    assert result.role is SimulationRole.BASELINE
    assert str(merged.frames["household"]["household_net_income"].dtype) == "float32"
    assert str(merged.frames["person"]["age"].dtype) == "int16"
    assert merged.frames["household"]["household_id"].tolist() == list(range(20))


def test_merge_rejects_missing_duplicate_and_cross_role_results() -> None:
    simulation = _planned_simulation(SimulationRole.BASELINE)
    results = _results(simulation)

    with pytest.raises(ValueError, match="incomplete"):
        merge_segment_results(simulation, results[:-1])
    with pytest.raises(ValueError, match="duplicate index"):
        merge_segment_results(simulation, [*results[:-1], results[0]])
    with pytest.raises(ValueError, match="another simulation role"):
        merge_segment_results(
            simulation,
            [
                results[0].model_copy(update={"role": SimulationRole.REFORM}),
                *results[1:],
            ],
        )


def test_merge_rejects_duplicate_entity_ids_across_groups() -> None:
    simulation = _planned_simulation(SimulationRole.BASELINE)
    results = _results(simulation)
    duplicate_payload, _ = serialize_simulation_frames(_frames(0))
    results[1] = results[1].model_copy(
        update={
            "parquet_payload": duplicate_payload,
            "payload_sha256": sha256(duplicate_payload).hexdigest(),
        }
    )

    with pytest.raises(ValueError, match="duplicate IDs"):
        merge_segment_results(simulation, results)
