"""Foundational tests for Stage 12-owned segmented execution."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from policyengine_simulation_executor.stage12_runtime.partition import (
    US_REGION_GROUPS,
    stage12_region_groups,
    stage12_region_groups_for_model,
)
from policyengine_simulation_executor.stage12_runtime.segment_contracts import (
    Stage12SegmentInput,
)
from test_stage12_runtime import _planned_simulation
from policyengine_simulation_contract.stage12_execution import SimulationRole

PROJECT = Path(__file__).resolve().parents[1]
PACKAGE = PROJECT / "src" / "policyengine_simulation_executor"
FORBIDDEN_IMPORTS = {
    "policyengine_simulation_executor.artifact_keys",
    "policyengine_simulation_executor.baseline_artifacts",
    "policyengine_simulation_executor.national_partition",
    "policyengine_simulation_executor.precompute",
    "policyengine_simulation_executor.precompute_models",
    "policyengine_simulation_executor.segmented_national_reduce",
    "src.modal._image_setup",
    "src.modal.precompute_app",
    "src.modal.segmented_national",
}


def test_us_partition_has_twenty_unique_groups_and_complete_state_coverage() -> None:
    groups = stage12_region_groups("us")

    assert groups is not None
    assert len(groups) == 20
    assert all(groups)
    flattened = [code for group in groups for code in group]
    assert len(flattened) == len(set(flattened)) == 51
    assert set(flattened) == {code for group in US_REGION_GROUPS for code in group}
    assert stage12_region_groups("uk") is None


def test_partition_returns_independent_mutable_copies() -> None:
    first = stage12_region_groups("us")
    second = stage12_region_groups("us")
    assert first is not None and second is not None

    first[-1].append("state/test")

    assert "state/test" not in second[-1]


def test_new_model_regions_are_added_to_the_final_group_in_canonical_order() -> None:
    model = SimpleNamespace(
        region_registry=SimpleNamespace(
            regions=[
                SimpleNamespace(code="state/zz", region_type="state"),
                SimpleNamespace(code="state/aa", region_type="state"),
                SimpleNamespace(code="district/1", region_type="district"),
            ]
        )
    )

    groups = stage12_region_groups_for_model("us", model)

    assert groups[-1][-2:] == ["state/aa", "state/zz"]


def test_segment_input_rejects_duplicate_or_non_state_regions() -> None:
    simulation = _planned_simulation(SimulationRole.BASELINE)

    with pytest.raises(ValidationError, match="must be unique"):
        Stage12SegmentInput(
            simulation=simulation,
            segment_index=0,
            region_codes=("state/ca", "state/ca"),
        )
    with pytest.raises(ValidationError, match="state regions"):
        Stage12SegmentInput(
            simulation=simulation,
            segment_index=0,
            region_codes=("district/1",),
        )


def test_stage12_feature_modules_do_not_import_v1_feature_implementations() -> None:
    feature_paths = list((PACKAGE / "stage12_runtime").glob("*.py"))
    cache_package = PACKAGE / "stage12_cache"
    if cache_package.exists():
        feature_paths.extend(cache_package.glob("*.py"))

    violations: list[str] = []
    for path in feature_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                continue
            for module in imported:
                if module in FORBIDDEN_IMPORTS:
                    violations.append(f"{path.name}: {module}")

    assert violations == []
