"""Tests for the reviewed Stage 12 annual-comparison adapter."""

from __future__ import annotations

from uuid import UUID

import pytest
from policyengine_simulation_contract.stage12_execution import SimulationRole
from stage12_fixtures import eligible_payload, worker

from policyengine_simulation_entry.stage12_adapter import (
    ComparisonSkipReason,
    adapt_annual_comparison,
)

EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000001")


def test_adapter_creates_two_strict_independent_simulation_inputs() -> None:
    result = adapt_annual_comparison(
        eligible_payload(),
        evaluation_id=EVALUATION_ID,
        worker=worker(),
    )

    assert result.eligible is True
    assert result.skip_reason is None
    assert result.report is not None
    assert result.report.evaluation_id == EVALUATION_ID
    assert result.report.baseline.role is SimulationRole.BASELINE
    assert result.report.reform.role is SimulationRole.REFORM
    assert (
        result.report.baseline.simulation_execution_id
        != result.report.reform.simulation_execution_id
    )
    assert result.report.baseline.policy == {}
    assert result.report.reform.policy == eligible_payload()["reform"]
    population = result.report.baseline.population
    assert population.kind == "dataset"
    assert population.artifact.uri.startswith("hf://")
    assert population.artifact.size_bytes is None
    assert result.report.baseline.bundle.dataset.identity == "populace_us_2024"


def test_absent_data_resolves_exact_bundle_default() -> None:
    payload = eligible_payload()
    assert "data" not in payload

    result = adapt_annual_comparison(
        payload,
        evaluation_id=EVALUATION_ID,
        worker=worker(),
    )

    assert result.report is not None
    assert result.report.baseline.bundle.dataset.artifact_revision.endswith("-revision")


def test_absent_package_versions_resolve_from_the_selected_bundle() -> None:
    payload = eligible_payload()
    payload.pop("version")
    payload.pop("policyengine_version")

    result = adapt_annual_comparison(
        payload,
        evaluation_id=EVALUATION_ID,
        worker=worker(),
    )

    assert result.report is not None
    bundle = result.report.baseline.bundle
    assert bundle.policyengine_version == "5.2.0"
    assert bundle.country_package_version == "1.764.6"


def test_normalized_production_telemetry_does_not_change_eligibility() -> None:
    payload = eligible_payload()
    payload["telemetry"] = {
        "run_id": "production-run-1",
        "process_id": "api-process-1",
        "capture_mode": "disabled",
    }

    result = adapt_annual_comparison(
        payload,
        evaluation_id=EVALUATION_ID,
        worker=worker(),
    )

    assert result.report is not None
    assert result.skip_reason is None
    assert result.report.baseline.options == {}
    assert result.report.reform.options == {}


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"scope": "household"}, ComparisonSkipReason.UNSUPPORTED_SCOPE),
        (
            {"include_cliffs": True},
            ComparisonSkipReason.UNSUPPORTED_CLIFF_CALCULATION,
        ),
        ({"country": "ca"}, ComparisonSkipReason.UNSUPPORTED_COUNTRY),
        ({"data": "another"}, ComparisonSkipReason.UNSUPPORTED_DATASET),
        ({"version": "another"}, ComparisonSkipReason.MISSING_BUNDLE_PROVENANCE),
        (
            {"policyengine_version": "another"},
            ComparisonSkipReason.MISSING_BUNDLE_PROVENANCE,
        ),
        ({"time_period": "2026-01"}, ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE),
        ({"region": None}, ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE),
        ({"segmented": True}, ComparisonSkipReason.UNSUPPORTED_OPTIONS),
        ({"region_group": ["state/ca"]}, ComparisonSkipReason.UNSUPPORTED_OPTIONS),
    ],
)
def test_adapter_returns_bounded_skip_reason(update, reason) -> None:
    payload = {**eligible_payload(), **update}

    result = adapt_annual_comparison(
        payload,
        evaluation_id=EVALUATION_ID,
        worker=worker(),
    )

    assert result.report is None
    assert result.skip_reason is reason
