"""Compact, versioned SPM calculation-receipt contracts."""

import json

import pytest
from pydantic import ValidationError

from policyengine_simulation_contract.spm import (
    SPMCalculationProvenance,
    SPMComparisonProvenance,
    SPMProvenance,
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_calculation_provenance,
    build_spm_comparison_provenance,
    build_spm_provenance,
)


SELECTION = SPMSelection(
    forecast_content_sha256="a" * 64,
    scenario="ce_trend",
    geography_kind="national",
    geography_id=None,
    county_vintage="2020",
    as_of=None,
)
VERSIONS = SPMRuntimeVersions.model_validate(
    {
        "policyengine": "6.2.1",
        "policyengine-core": "3.32.10",
        "policyengine-us": "2.2.1",
        "spm-calculator": "1.0.0",
    }
)


def receipt(*, year: str = "2026", forecast_id: str = "forecast-2026"):
    return build_spm_provenance(
        forecast_id=forecast_id,
        forecast_sha256="a" * 64,
        selection=SELECTION,
        years=(year,),
        runtime_versions=VERSIONS,
    )


def test_compact_receipt_has_exact_public_shape() -> None:
    value = receipt().model_dump(mode="json", by_alias=True)

    assert value == {
        "schema_version": "canonical-spm-provenance-v2",
        "forecast_id": "forecast-2026",
        "forecast_sha256": "a" * 64,
        "scenario": "ce_trend",
        "geography_kind": "national",
        "geography_id": None,
        "county_vintage": "2020",
        "as_of": None,
        "years": ["2026"],
        "runtime_versions": {
            "policyengine": "6.2.1",
            "policyengine-core": "3.32.10",
            "policyengine-us": "2.2.1",
            "spm-calculator": "1.0.0",
        },
    }
    assert len(json.dumps(value, separators=(",", ":")).encode()) < 1_024


@pytest.mark.parametrize(
    "updates",
    [
        {"forecast_sha256": "A" * 64},
        {"forecast_sha256": "a" * 63},
        {"forecast_id": "forecast with spaces"},
        {"years": ["2027", "2026"]},
        {"years": ["2026", "2026"]},
        {"years": ["26"]},
        {"geography_kind": "state"},
        {"geography_kind": "metro", "geography_id": None},
        {"geography_kind": "national", "geography_id": "35620"},
        {"county_vintage": "20"},
        {"as_of": "09/01/2026"},
    ],
)
def test_compact_receipt_rejects_invalid_values(updates) -> None:
    value = receipt().model_dump(mode="json", by_alias=True)
    value.update(updates)

    with pytest.raises(ValidationError):
        SPMProvenance.model_validate(value)


def test_compact_receipt_rejects_old_rich_shape() -> None:
    old = {
        "forecast_id": "forecast-2026",
        "forecast_sha256": "a" * 64,
        "scenario": "ce_trend",
        "geography_kind": "national",
        "runtime_versions": {"policyengine-us": "2.2.1"},
        "years": {"2026": {"median_diagnostics": {}}},
        "geographies": [],
        "composition_method": "classified-inputs",
        "storage_method": "formula",
    }

    with pytest.raises(ValidationError):
        SPMProvenance.model_validate(old)


def test_compact_receipt_serializes_as_of_as_an_iso_date() -> None:
    dated = build_spm_provenance(
        forecast_id="forecast-2026",
        forecast_sha256="a" * 64,
        selection=SELECTION.model_copy(
            update={"as_of": "2026-09-09"},
        ),
        years=("2026",),
        runtime_versions=VERSIONS,
    )

    assert dated.model_dump(mode="json", by_alias=True)["as_of"] == "2026-09-09"


def test_builder_requires_receipt_to_match_resolved_selection() -> None:
    with pytest.raises(ValueError, match="artifact hash"):
        build_spm_provenance(
            forecast_id="forecast-2026",
            forecast_sha256="b" * 64,
            selection=SELECTION,
            years=("2026",),
            runtime_versions=VERSIONS,
        )


def test_calculation_builder_requires_complete_matching_config() -> None:
    calculated = build_spm_calculation_provenance(
        config=SELECTION,
        receipt=receipt(),
    )
    assert isinstance(calculated, SPMCalculationProvenance)

    with pytest.raises(ValueError, match="scenario"):
        build_spm_calculation_provenance(
            config=SELECTION.model_copy(update={"scenario": "zero_real"}),
            receipt=receipt(),
        )


def test_comparison_collapses_identical_children_and_counts_them() -> None:
    comparison = build_spm_comparison_provenance(
        baseline_receipts=[receipt()] * 20,
        reform_receipts=[receipt()] * 20,
    )
    value = comparison.model_dump(mode="json", by_alias=True)

    assert value["schema_version"] == "canonical-spm-comparison-v2"
    assert value["baseline"]["execution_count"] == 20
    assert value["reform"]["execution_count"] == 20
    assert len(json.dumps(value, separators=(",", ":")).encode()) < 2_048


def test_comparison_rejects_missing_or_mismatched_children() -> None:
    with pytest.raises(ValueError, match="baseline receipts"):
        build_spm_comparison_provenance(
            baseline_receipts=[],
            reform_receipts=[receipt()],
        )
    with pytest.raises(ValueError, match="baseline receipts differ"):
        build_spm_comparison_provenance(
            baseline_receipts=[receipt(), receipt(forecast_id="other")],
            reform_receipts=[receipt()],
        )
    with pytest.raises(ValueError, match="baseline and reform"):
        build_spm_comparison_provenance(
            baseline_receipts=[receipt()],
            reform_receipts=[receipt(year="2027")],
        )


def test_comparison_contract_rejects_old_receipt_lists() -> None:
    with pytest.raises(ValidationError):
        SPMComparisonProvenance.model_validate(
            {"baseline": [receipt()], "reform": [receipt()]}
        )
