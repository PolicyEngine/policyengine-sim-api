"""Tests for typed Stage 12 execution contracts."""

from __future__ import annotations

from typing import cast
from uuid import UUID

import pytest
from pydantic import JsonValue

from policyengine_simulation_contract.spm import (
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_comparison_provenance,
    build_spm_provenance,
)
from policyengine_simulation_contract.stage12_execution import (
    SIMULATION_PARQUET_PAYLOAD_CONTRACT,
    AggregateReportArtifactPayload,
    BundleProvenance,
    DatasetProvenance,
    EntityOutputPlan,
    ReportAggregate,
    ReportOutputRequirements,
    SimulationArtifactDescriptor,
    Stage12OutputPlan,
    stage12_output_plan_sha256,
)
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
    UKLocalAuthorityMetadata,
)


def _plan() -> Stage12OutputPlan:
    return Stage12OutputPlan(
        country="us",
        requirements=ReportOutputRequirements(
            aggregates=tuple(ReportAggregate),
            include_cliff_impacts=False,
            labor_supply_response_active=False,
        ),
        entities=(
            EntityOutputPlan(
                entity="household",
                materialized_variables=("household_id", "household_net_income"),
                additional_variables=(),
            ),
            EntityOutputPlan(
                entity="person",
                materialized_variables=("federal_benefit_cost", "person_id"),
                additional_variables=("federal_benefit_cost",),
            ),
        ),
    )


def _bundle() -> BundleProvenance:
    return BundleProvenance(
        policyengine_version="6.2.1",
        country_package_name="policyengine-us",
        country_package_version="2.2.1",
        dataset=DatasetProvenance(
            identity="populace_us_2024",
            uri="hf://policyengine/data/populace_us_2024.h5@revision",
            artifact_revision="revision",
            data_package_name="populace-data",
            data_package_version="0.1.0",
        ),
        bundle_manifest_sha256="b" * 64,
    )


def _spm_comparison() -> dict[str, JsonValue]:
    selection = SPMSelection(
        forecast_content_sha256="a" * 64,
        scenario="official",
        geography_kind="national",
        geography_id=None,
        county_vintage="2020",
        as_of=None,
    )
    receipt = build_spm_provenance(
        forecast_id="forecast-2026",
        forecast_sha256="a" * 64,
        selection=selection,
        years=("2026",),
        runtime_versions=SPMRuntimeVersions.model_validate(
            {
                "policyengine": "6.2.1",
                "policyengine-core": "3.32.10",
                "policyengine-us": "2.2.1",
                "spm-calculator": "1.0.0",
            }
        ),
    )
    return cast(
        dict[str, JsonValue],
        build_spm_comparison_provenance(
            baseline_receipts=(receipt,),
            reform_receipts=(receipt,),
        ).model_dump(mode="json", by_alias=True),
    )


def test_output_plan_has_a_deterministic_digest() -> None:
    plan = _plan()

    assert stage12_output_plan_sha256(plan) == stage12_output_plan_sha256(
        Stage12OutputPlan.model_validate(plan.model_dump(mode="json"))
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "materialized_variables",
            ("person_id", "federal_benefit_cost"),
            "canonical order",
        ),
        (
            "materialized_variables",
            ("person_id", "person_id"),
            "unique",
        ),
        (
            "additional_variables",
            ("missing",),
            "subset",
        ),
        (
            "dataset_variables",
            ("missing",),
            "subset",
        ),
    ],
)
def test_entity_output_plan_rejects_noncanonical_variables(
    field: str,
    value: tuple[str, ...],
    message: str,
) -> None:
    values = {
        "entity": "person",
        "materialized_variables": ("federal_benefit_cost", "person_id"),
        "additional_variables": ("federal_benefit_cost",),
        "dataset_variables": (),
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        EntityOutputPlan.model_validate(values)


def test_output_plan_rejects_duplicate_or_unsorted_entities() -> None:
    entity = _plan().entities[0]

    with pytest.raises(ValueError, match="canonical order"):
        Stage12OutputPlan(
            country="us",
            requirements=_plan().requirements,
            entities=(
                _plan().entities[1],
                entity,
            ),
        )

    with pytest.raises(ValueError, match="unique"):
        Stage12OutputPlan(
            country="us",
            requirements=_plan().requirements,
            entities=(entity, entity),
        )


def test_entity_output_plan_separates_calculated_and_dataset_variables() -> None:
    with pytest.raises(ValueError, match="must be disjoint"):
        EntityOutputPlan(
            entity="household",
            materialized_variables=("constituency_code_oa", "household_id"),
            additional_variables=("constituency_code_oa",),
            dataset_variables=("constituency_code_oa",),
        )


def test_uk_local_authority_metadata_is_strict_and_typed() -> None:
    metadata = UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD22
    )

    assert metadata.country == "uk"
    assert metadata.boundary_version is UKLocalAuthorityBoundaryVersion.LAD22

    with pytest.raises(ValueError, match="extra_forbidden"):
        UKLocalAuthorityMetadata.model_validate(
            {"country": "uk", "boundary_version": "lad23", "dataset": "microcosm"}
        )


def test_parquet_contract_names_uk_local_authority_metadata() -> None:
    assert (
        SIMULATION_PARQUET_PAYLOAD_CONTRACT.uk_local_authority_metadata_key
        == "policyengine.stage12.uk_local_authority_metadata"
    )


def test_simulation_artifact_schema_uses_the_receipt_without_a_wrapper() -> None:
    schema_text = str(SimulationArtifactDescriptor.model_json_schema())

    assert "SPMProvenance" in schema_text
    assert "SPMCalculationProvenance" not in schema_text


def test_aggregate_artifact_accepts_only_canonical_spm_provenance() -> None:
    comparison = _spm_comparison()
    artifact = AggregateReportArtifactPayload(
        evaluation_id=UUID("00000000-0000-0000-0000-000000000001"),
        requested_aggregates=(ReportAggregate.BUDGET,),
        bundle=_bundle(),
        result={"spm_provenance": comparison},
    )

    assert artifact.result["spm_provenance"] == comparison

    with pytest.raises(ValueError, match="legacy spm_config"):
        AggregateReportArtifactPayload(
            evaluation_id=artifact.evaluation_id,
            requested_aggregates=artifact.requested_aggregates,
            bundle=artifact.bundle,
            result={"spm_config": {}},
        )

    mismatched = _spm_comparison()
    reform = cast(dict[str, JsonValue], mismatched["reform"])
    receipt = cast(dict[str, JsonValue], reform["receipt"])
    receipt["years"] = ["2027"]
    with pytest.raises(ValueError, match="baseline and reform"):
        AggregateReportArtifactPayload(
            evaluation_id=artifact.evaluation_id,
            requested_aggregates=artifact.requested_aggregates,
            bundle=artifact.bundle,
            result={"spm_provenance": mismatched},
        )
