"""Tests for the reviewed Stage 12 annual-comparison adapter."""

from __future__ import annotations

from uuid import UUID

import pytest
from policyengine_simulation_contract.stage12_execution import SimulationRole
from policyengine_simulation_entry.stage12_adapter import (
    ComparisonSkipReason,
    adapt_annual_comparison,
)
from stage12_fixtures import eligible_payload, worker

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
        "submission_claim_id": "api-process-1",
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


def test_cliff_analysis_is_forwarded_to_both_simulations() -> None:
    payload = {**eligible_payload(), "include_cliffs": True}

    result = adapt_annual_comparison(
        payload,
        evaluation_id=EVALUATION_ID,
        worker=worker(),
    )

    assert result.report is not None
    assert result.skip_reason is None
    assert result.report.baseline.options == {"include_cliffs": True}
    assert result.report.reform.options == {"include_cliffs": True}


@pytest.mark.parametrize("segmented", [True, False])
def test_segmentation_selection_is_forwarded_to_both_simulations(segmented) -> None:
    payload = {**eligible_payload(), "segmented": segmented}

    result = adapt_annual_comparison(
        payload,
        evaluation_id=EVALUATION_ID,
        worker=worker(),
    )

    assert result.report is not None
    assert result.report.baseline.options == {"segmented": segmented}
    assert result.report.reform.options == {"segmented": segmented}


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"scope": "household"}, ComparisonSkipReason.UNSUPPORTED_SCOPE),
        (
            {"include_cliffs": "true"},
            ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE,
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
        ({"segmented": "true"}, ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE),
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


def _uk_payload(**fields: object) -> dict:
    return {
        **eligible_payload(),
        "country": "uk",
        "region": "uk",
        "version": "2.90.2",
        **fields,
    }


def test_a_local_area_breakdown_gets_no_comparison_run() -> None:
    # No Stage 12 report computes a local-area breakdown alone.
    result = adapt_annual_comparison(
        _uk_payload(breakdown="local_areas"),
        evaluation_id=EVALUATION_ID,
        worker=worker(),
    )

    assert result.report is None
    assert result.skip_reason is ComparisonSkipReason.UNSUPPORTED_OPTIONS


def test_an_absent_breakdown_keeps_a_uk_request_eligible() -> None:
    result = adapt_annual_comparison(
        _uk_payload(breakdown=None),
        evaluation_id=EVALUATION_ID,
        worker=worker(),
    )

    assert result.skip_reason is None
    assert result.report is not None


def _expected_simulation(role: str, execution_id: str, policy: dict) -> dict:
    dataset_uri = "hf://policyengine/data/populace_us_2024.h5@populace_us_2024-revision"
    return {
        "contract_version": 1,
        "evaluation_id": str(EVALUATION_ID),
        "simulation_execution_id": execution_id,
        "role": role,
        "policy": policy,
        "population": {
            "kind": "dataset",
            "artifact": {
                "uri": dataset_uri,
                "media_type": "application/x-hdf5",
                "content_sha256": "a" * 64,
                "size_bytes": None,
            },
        },
        "year": 2026,
        "geography": {
            "country": "us",
            "region": "us",
            "filter_field": None,
            "filter_value": None,
            "filter_strategy": None,
        },
        "options": {"spm": {"geography_kind": "national"}, "include_cliffs": True},
        "bundle": {
            "policyengine_version": "5.2.0",
            "country_package_name": "policyengine-us",
            "country_package_version": "1.764.6",
            "dataset": {
                "identity": "populace_us_2024",
                "uri": dataset_uri,
                "artifact_revision": "populace_us_2024-revision",
                "data_package_name": "populace-data",
                "data_package_version": "0.1.0",
            },
            "bundle_manifest_sha256": "b" * 64,
        },
    }


@pytest.mark.parametrize(
    "data",
    [
        "default",
        "populace_us_2024",
        "hf://policyengine/data/populace_us_2024.h5@populace_us_2024-revision",
        "populace_us_2024@populace_us_2024-revision",
    ],
)
def test_adapter_builds_the_complete_report_input(data) -> None:
    payload = {
        **eligible_payload(),
        "include_cliffs": True,
        "spm": {"geography_kind": "national"},
        "data": data,
    }

    result = adapt_annual_comparison(
        payload,
        evaluation_id=EVALUATION_ID,
        worker=worker(),
    )

    assert result.skip_reason is None
    assert result.report is not None
    assert result.report.model_dump(mode="json") == {
        "contract_version": 1,
        "evaluation_id": str(EVALUATION_ID),
        "baseline": _expected_simulation(
            "baseline", "20eaf597-1c15-549e-a96e-3661052c3f31", {}
        ),
        "reform": _expected_simulation(
            "reform",
            "5a17bbb2-20a6-5789-a508-9d26900587e4",
            {
                "gov.irs.credits.ctc.amount.base[0].amount": {
                    "2026-01-01.2100-12-31": 3000
                }
            },
        ),
        "requested_aggregates": [
            "budget",
            "poverty",
            "inequality",
            "distributional",
            "winners_and_losers",
            "geographic",
            "program_statistics",
        ],
    }


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"region_group": None}, None),
        ({"spm": None, "include_cliffs": None, "segmented": False}, None),
        ({"scope": None}, ComparisonSkipReason.UNSUPPORTED_SCOPE),
        ({"baseline": None}, ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE),
        ({"reform": []}, ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE),
        ({"time_period": 2026}, ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE),
        ({"time_period": "20a6"}, ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE),
        ({"time_period": "02026"}, ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE),
        ({"region": ""}, ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE),
        ({"country": None}, ComparisonSkipReason.UNSUPPORTED_COUNTRY),
        (
            {"region_group": ["state/ca"], "scope": "household"},
            ComparisonSkipReason.UNSUPPORTED_OPTIONS,
        ),
        (
            {"scope": "household", "include_cliffs": "true"},
            ComparisonSkipReason.UNSUPPORTED_SCOPE,
        ),
        (
            {"include_cliffs": "true", "segmented": True},
            ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE,
        ),
        (
            {"segmented": "true", "country": "ca"},
            ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE,
        ),
        (
            {"country": "ca", "policyengine_version": "another"},
            ComparisonSkipReason.UNSUPPORTED_COUNTRY,
        ),
        (
            {"policyengine_version": "another", "baseline": None},
            ComparisonSkipReason.MISSING_BUNDLE_PROVENANCE,
        ),
        (
            {"version": "another", "time_period": "2026-01"},
            ComparisonSkipReason.MISSING_BUNDLE_PROVENANCE,
        ),
        (
            {"baseline": None, "time_period": "2026-01"},
            ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE,
        ),
        (
            {"region": None, "data": "another"},
            ComparisonSkipReason.UNSUPPORTED_REQUEST_SHAPE,
        ),
    ],
)
def test_adapter_checks_eligibility_in_order(update, reason) -> None:
    payload = {**eligible_payload(), **update}

    result = adapt_annual_comparison(
        payload,
        evaluation_id=EVALUATION_ID,
        worker=worker(),
    )

    assert result.skip_reason is reason
    assert result.eligible is (reason is None)


def _datasets_named(report) -> set[tuple[str, str, str, str, str]]:
    return {
        (
            simulation.bundle.dataset.identity,
            simulation.bundle.dataset.uri,
            simulation.bundle.dataset.artifact_revision,
            simulation.population.artifact.uri,
            simulation.population.artifact.content_sha256,
        )
        for simulation in (report.baseline, report.reform)
    }


@pytest.mark.parametrize(
    "region", ["state/ut", "State/UT", "ut", " ut ", "congressional_district/UT-01"]
)
def test_state_and_district_requests_name_the_regional_dataset(region) -> None:
    from stage12_fixtures import (
        REGIONAL_DATASET,
        REGIONAL_REVISION,
        REGIONAL_URI,
        regional_worker,
    )

    result = adapt_annual_comparison(
        {**eligible_payload(), "region": region},
        evaluation_id=EVALUATION_ID,
        worker=regional_worker(),
    )

    assert result.skip_reason is None
    assert _datasets_named(result.report) == {
        (REGIONAL_DATASET, REGIONAL_URI, REGIONAL_REVISION, REGIONAL_URI, "c" * 64)
    }
    assert result.report.baseline.geography.region == region


def test_a_national_request_keeps_the_default_dataset_with_regional_workers() -> None:
    from stage12_fixtures import regional_worker

    payload = {**eligible_payload(), "include_cliffs": True}
    payload["spm"] = {"geography_kind": "national"}

    national = adapt_annual_comparison(
        payload, evaluation_id=EVALUATION_ID, worker=regional_worker()
    )
    today = adapt_annual_comparison(
        payload, evaluation_id=EVALUATION_ID, worker=worker()
    )

    assert national.report.model_dump(mode="json") == today.report.model_dump(
        mode="json"
    )


def test_a_worker_without_region_datasets_gets_the_default_dataset() -> None:
    result = adapt_annual_comparison(
        {**eligible_payload(), "region": "state/ut"},
        evaluation_id=EVALUATION_ID,
        worker=worker(),
    )

    assert result.skip_reason is None
    assert {item[0] for item in _datasets_named(result.report)} == {"populace_us_2024"}


def test_a_uk_request_keeps_the_default_dataset_with_regional_workers() -> None:
    from stage12_fixtures import regional_worker

    result = adapt_annual_comparison(
        {
            **eligible_payload(),
            "country": "uk",
            "region": "country/scotland",
            "version": "2.90.2",
        },
        evaluation_id=EVALUATION_ID,
        worker=regional_worker(),
    )

    assert result.skip_reason is None
    assert {item[0] for item in _datasets_named(result.report)} == {"populace_uk_2023"}
