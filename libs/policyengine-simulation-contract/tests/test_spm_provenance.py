"""Compact, versioned SPM calculation-receipt contracts."""

import json

import pytest
from policyengine_simulation_contract.spm import (
    SPMComparisonProvenance,
    SPMProvenance,
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_comparison_provenance,
    build_spm_provenance,
    validate_spm_calculation_provenance,
)
from pydantic import ValidationError

SELECTION = SPMSelection(
    forecast_content_sha256="a" * 64,
    scenario="ce_trend",
    geography_kind="national",
    geography_id=None,
    county_vintage="2020",
    as_of=None,
)
# Synthetic fixture values used only to exercise the public receipt contract.
# Deployed workers obtain real package versions from the PolicyEngine.py bundle.
TEST_RUNTIME_VERSIONS = SPMRuntimeVersions.model_validate(
    {
        "policyengine": "0.0.0-test-policyengine",
        "policyengine-core": "0.0.0-test-policyengine-core",
        "policyengine-us": "0.0.0-test-policyengine-us",
        "spm-calculator": "0.0.0-test-spm-calculator",
    }
)


def receipt(*, year: str = "2026", forecast_id: str = "forecast-2026"):
    return build_spm_provenance(
        forecast_id=forecast_id,
        forecast_sha256="a" * 64,
        selection=SELECTION,
        years=(year,),
        runtime_versions=TEST_RUNTIME_VERSIONS,
    )


def test_compact_receipt_has_exact_public_shape() -> None:
    value = receipt().model_dump(mode="json", by_alias=True)
    without_optional_nulls = receipt().model_dump(
        mode="json", by_alias=True, exclude_none=True
    )

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
            "policyengine": "0.0.0-test-policyengine",
            "policyengine-core": "0.0.0-test-policyengine-core",
            "policyengine-us": "0.0.0-test-policyengine-us",
            "spm-calculator": "0.0.0-test-spm-calculator",
        },
    }
    assert without_optional_nulls["geography_id"] is None
    assert without_optional_nulls["as_of"] is None
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


@pytest.mark.parametrize(
    "package",
    ["policyengine", "policyengine-core", "policyengine-us", "spm-calculator"],
)
@pytest.mark.parametrize("invalid_value", [None, ""])
def test_runtime_versions_reject_invalid_package_versions(
    package: str, invalid_value: str | None
) -> None:
    versions = TEST_RUNTIME_VERSIONS.model_dump(mode="json", by_alias=True)
    invalid_versions: dict[str, str | None] = {**versions, package: invalid_value}

    with pytest.raises(ValidationError):
        SPMRuntimeVersions.model_validate(invalid_versions)


def test_compact_receipt_serializes_as_of_as_an_iso_date() -> None:
    dated = build_spm_provenance(
        forecast_id="forecast-2026",
        forecast_sha256="a" * 64,
        selection=SELECTION.model_copy(
            update={"as_of": "2026-09-09"},
        ),
        years=("2026",),
        runtime_versions=TEST_RUNTIME_VERSIONS,
    )

    assert dated.model_dump(mode="json", by_alias=True)["as_of"] == "2026-09-09"


def test_builder_requires_receipt_to_match_resolved_selection() -> None:
    with pytest.raises(ValueError, match="artifact hash"):
        build_spm_provenance(
            forecast_id="forecast-2026",
            forecast_sha256="b" * 64,
            selection=SELECTION,
            years=("2026",),
            runtime_versions=TEST_RUNTIME_VERSIONS,
        )


def test_calculation_builder_requires_complete_matching_config() -> None:
    calculated = validate_spm_calculation_provenance(
        config=SELECTION,
        receipt=receipt(),
    )
    assert calculated == receipt()
    assert calculated.model_dump(mode="json", by_alias=True) == receipt().model_dump(
        mode="json", by_alias=True
    )

    with pytest.raises(ValueError, match="scenario"):
        validate_spm_calculation_provenance(
            config=SELECTION.model_copy(update={"scenario": "zero_real"}),
            receipt=receipt(),
        )


def test_calculation_provenance_rejects_legacy_sibling_config() -> None:
    calculated = validate_spm_calculation_provenance(
        config=SELECTION,
        receipt=receipt(),
    ).model_dump(mode="json", by_alias=True)
    calculated["spm_config"] = SELECTION.model_dump(mode="json")

    with pytest.raises(ValidationError):
        SPMProvenance.model_validate(calculated)


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


def test_comparison_model_rejects_mismatched_receipts() -> None:
    comparison = build_spm_comparison_provenance(
        baseline_receipts=[receipt()],
        reform_receipts=[receipt()],
    ).model_dump(mode="json", by_alias=True)
    comparison["reform"]["receipt"]["years"] = ["2027"]

    with pytest.raises(ValidationError, match="baseline and reform"):
        SPMComparisonProvenance.model_validate(comparison)


def test_comparison_contract_rejects_old_receipt_lists() -> None:
    with pytest.raises(ValidationError):
        SPMComparisonProvenance.model_validate(
            {"baseline": [receipt()], "reform": [receipt()]}
        )
