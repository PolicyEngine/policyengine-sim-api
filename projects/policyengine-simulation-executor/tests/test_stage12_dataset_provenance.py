"""Tests for the dataset a Stage 12 calculation loads, and the provenance it checks."""

from __future__ import annotations

import pandas as pd
import pytest
from policyengine_simulation_contract.stage12_execution import (
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    GeographySelection,
    SimulationRole,
)
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
    UKLocalAuthorityMetadata,
)
from policyengine_simulation_executor.stage12_runtime.aggregation import (
    validate_uk_local_authority_metadata,
)
from policyengine_simulation_executor.uk_local_authority_metadata import (
    detect_uk_local_authority_metadata,
)
from stage12_runtime_support import (
    bundle_provenance,
    planned_simulation,
    run_until_dataset_loaded,
)

REGIONAL_URI = "hf://policyengine/data/populace_us_2024_acs_local.h5@regional-revision"


def _regional_provenance() -> BundleProvenance:
    return bundle_provenance().model_copy(
        update={
            "dataset": DatasetProvenance(
                identity="populace_us_2024_acs_local",
                uri=REGIONAL_URI,
                artifact_revision="regional-revision",
                data_package_name="populace-data",
                data_package_version="0.1.0",
            )
        }
    )


def _installed_bundle_with_regions():
    from policyengine_simulation_contract.stage12_bundle import (
        ResolvedStage12Bundle,
        Stage12BundleManifest,
        Stage12CountryBundle,
        Stage12Dataset,
        Stage12RegionDataset,
    )

    national = Stage12Dataset(
        identity="populace_us_2024",
        uri=bundle_provenance().dataset.uri,
        artifact_revision="revision",
        sha256="a" * 64,
        repo_type="dataset",
    )
    regional = Stage12Dataset(
        identity="populace_us_2024_acs_local",
        uri=REGIONAL_URI,
        artifact_revision="regional-revision",
        sha256="c" * 64,
        repo_type="dataset",
    )
    return ResolvedStage12Bundle(
        bundle=Stage12BundleManifest(
            policyengine_version="5.2.0",
            policyengine_requirement="policyengine==5.2.0",
            core_package_version="3.0.0",
            core_package_requirement="policyengine-core==3.0.0",
            countries=(
                Stage12CountryBundle(
                    country="us",
                    country_package_name="policyengine-us",
                    country_package_version="1.764.6",
                    country_package_requirement="policyengine-us==1.764.6",
                    data_package_name="populace-data",
                    data_package_version="0.1.0",
                    data_release_version="revision",
                    data_artifact_revision="revision",
                    default_dataset=national.identity,
                    default_dataset_uri=national.uri,
                    datasets=(national, regional),
                    region_datasets=(
                        Stage12RegionDataset(
                            region_type="national", dataset=national.identity
                        ),
                        Stage12RegionDataset(
                            region_type="state", dataset=regional.identity
                        ),
                        Stage12RegionDataset(
                            region_type="congressional_district",
                            dataset=regional.identity,
                        ),
                    ),
                ),
            ),
        ),
        bundle_manifest_sha256="b" * 64,
    )


def _simulation_for(region: str, provenance: BundleProvenance):
    simulation = planned_simulation(SimulationRole.BASELINE)
    return simulation.model_copy(
        update={
            "geography": GeographySelection(country="us", region=region),
            "bundle": provenance,
            "population": DatasetPopulationInput(
                artifact=DatasetArtifactReference(
                    uri=provenance.dataset.uri,
                    media_type=DatasetArtifactMediaType.HDF5,
                    content_sha256="a" * 64,
                )
            ),
        }
    )


@pytest.mark.parametrize(
    ("region", "provenance"),
    [
        ("state/ut", _regional_provenance),
        ("State/UT", _regional_provenance),
        ("ut", _regional_provenance),
        ("congressional_district/UT-01", _regional_provenance),
        ("us", bundle_provenance),
        # Entry services released before regional provenance send the default
        # dataset for state runs until they are updated.
        ("state/ut", bundle_provenance),
    ],
)
def test_installed_bundle_accepts_the_dataset_a_region_loads(
    monkeypatch, region, provenance
) -> None:
    from policyengine_simulation_executor.stage12_runtime import simulation as worker

    monkeypatch.setattr(worker, "load_stage12_bundle", _installed_bundle_with_regions)

    worker._require_installed_bundle(_simulation_for(region, provenance()))


def test_uk_regions_keep_the_default_dataset_when_the_bundle_names_region_types(
    monkeypatch,
) -> None:
    from policyengine_simulation_contract.stage12_bundle import Stage12RegionDataset
    from policyengine_simulation_executor.stage12_runtime import simulation as worker

    resolved = _installed_bundle_with_regions()
    us = resolved.bundle.countries[0]
    uk = us.model_copy(
        update={
            "country": "uk",
            "region_datasets": (
                Stage12RegionDataset(
                    region_type="national", dataset=us.default_dataset
                ),
            ),
        }
    )
    resolved = resolved.model_copy(
        update={"bundle": resolved.bundle.model_copy(update={"countries": (us, uk)})}
    )
    monkeypatch.setattr(worker, "load_stage12_bundle", lambda: resolved)
    simulation = _simulation_for("us", bundle_provenance()).model_copy(
        update={"geography": GeographySelection(country="uk", region="country/wales")}
    )

    worker._require_installed_bundle(simulation)
    with pytest.raises(RuntimeError, match="differs from the installed bundle"):
        worker._require_installed_bundle(
            simulation.model_copy(update={"bundle": _regional_provenance()})
        )


@pytest.mark.parametrize(
    ("region", "dataset"),
    [
        ("us", "populace_us_2024_acs_local"),
        ("state/ut", "source_enrichment"),
    ],
)
def test_installed_bundle_refuses_a_dataset_the_region_does_not_load(
    monkeypatch, region, dataset
) -> None:
    from policyengine_simulation_executor.stage12_runtime import simulation as worker

    monkeypatch.setattr(worker, "load_stage12_bundle", _installed_bundle_with_regions)
    provenance = _regional_provenance()
    provenance = provenance.model_copy(
        update={"dataset": provenance.dataset.model_copy(update={"identity": dataset})}
    )

    with pytest.raises(RuntimeError, match="differs from the installed bundle"):
        worker._require_installed_bundle(_simulation_for(region, provenance))


def _selection(name: str, uri: str, *, is_default: bool):
    from policyengine_simulation_executor.simulation_runtime import DatasetSelection

    return DatasetSelection(name=name, uri=uri, is_default=is_default)


def test_a_calculation_loads_the_dataset_its_provenance_names(monkeypatch) -> None:
    from policyengine_simulation_executor.simulation_runtime import RegionResolution

    loaded = run_until_dataset_loaded(
        monkeypatch,
        region=RegionResolution(code="state/ut", state_code="UT"),
        selection=_selection(
            "populace_us_2024_acs_local", REGIONAL_URI, is_default=False
        ),
        baked_folder=None,
        planned=_simulation_for("state/ut", _regional_provenance()),
    )

    assert loaded.loads[0]["selection"].name == "populace_us_2024_acs_local"


def test_a_calculation_refuses_provenance_naming_another_dataset(
    monkeypatch,
) -> None:
    from policyengine_simulation_executor.simulation_runtime import RegionResolution

    with pytest.raises(RuntimeError, match="dataset its region loads"):
        run_until_dataset_loaded(
            monkeypatch,
            region=RegionResolution(code="state/ut", state_code="UT"),
            selection=_selection(
                "populace_us_2024", bundle_provenance().dataset.uri, is_default=True
            ),
            baked_folder=None,
            planned=_simulation_for("state/ut", _regional_provenance()),
        )


def test_default_provenance_from_an_older_entry_service_is_still_loaded(
    monkeypatch,
) -> None:
    from policyengine_simulation_executor.simulation_runtime import RegionResolution

    loaded = run_until_dataset_loaded(
        monkeypatch,
        region=RegionResolution(code="state/ut", state_code="UT"),
        selection=_selection(
            "populace_us_2024_acs_local", REGIONAL_URI, is_default=False
        ),
        baked_folder=None,
        planned=_simulation_for("state/ut", bundle_provenance()),
    )

    assert loaded.loads[0]["selection"].name == "populace_us_2024_acs_local"


def test_uk_boundary_version_is_detected_from_the_unscoped_dataset() -> None:
    dataset = type(
        "Dataset",
        (),
        {
            "data": type(
                "Data",
                (),
                {
                    "entity_data": {
                        "household": pd.DataFrame(
                            {"la_code_oa": ["E06000001", "E07000026"]}
                        )
                    }
                },
            )()
        },
    )()

    metadata = detect_uk_local_authority_metadata("uk", dataset)

    assert metadata == UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD22
    )


def test_country_metadata_alignment_rejects_missing_or_mismatched_uk_values() -> None:
    lad22 = UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD22
    )
    lad23 = UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD23
    )

    with pytest.raises(ValueError, match="missing"):
        validate_uk_local_authority_metadata("uk", None, lad22)
    with pytest.raises(ValueError, match="do not match"):
        validate_uk_local_authority_metadata("uk", lad22, lad23)
    assert validate_uk_local_authority_metadata("uk", lad22, lad22) == lad22
    assert validate_uk_local_authority_metadata("us", None, None) is None


@pytest.mark.parametrize(
    ("region_code", "routed", "accepted"),
    [
        ("uk", True, True),
        ("country/england", True, True),
        ("uk", False, False),
        ("constituency/E14001063", True, False),
        ("local_authority/E06000063", True, False),
        (None, True, False),
    ],
)
def test_uk_artifacts_without_metadata_need_a_routed_national_request(
    monkeypatch, region_code: str | None, *, routed: bool, accepted: bool
) -> None:
    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "uk_area_regions_routed",
        lambda: routed,
    )

    if accepted:
        assert (
            validate_uk_local_authority_metadata(
                "uk", None, None, region_code=region_code
            )
            is None
        )
    else:
        with pytest.raises(ValueError, match="metadata is missing"):
            validate_uk_local_authority_metadata(
                "uk", None, None, region_code=region_code
            )
