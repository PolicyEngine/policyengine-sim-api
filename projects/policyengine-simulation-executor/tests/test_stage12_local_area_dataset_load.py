"""Tests for a UK-wide or nation simulation calculated on the local-area dataset."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from policyengine_simulation_contract.stage12_bundle import (
    ResolvedStage12Bundle,
    Stage12BundleManifest,
    Stage12CountryBundle,
    Stage12Dataset,
    Stage12RegionDataset,
)
from policyengine_simulation_contract.stage12_execution import (
    LOCAL_AREA_AGGREGATES,
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    EntityOutputPlan,
    GeographySelection,
    PlannedSimulationExecutionInput,
    ReportOutputRequirements,
    SimulationRole,
    Stage12OutputPlan,
)
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
)
from policyengine_simulation_executor.simulation_runtime import (
    DatasetSelection,
    RegionResolution,
)
from policyengine_simulation_executor.stage12_runtime import (
    local_area_datasets,
)
from policyengine_simulation_executor.stage12_runtime import simulation as worker
from stage12_runtime_support import BASELINE_ID, EVALUATION_ID

NATIONAL_DATASET = "microcosm_uk_2024_25"
LOCAL_AREA_DATASET = "microcosm_uk_2024_25_dense"
POLICYENGINE_VERSION = "6.17.1"
COUNTRY_PACKAGE_VERSION = "2.174.1"
BUNDLE_MANIFEST_SHA256 = "b" * 64
# Wales as .py's region registry resolves it on the national dataset: the
# region's own dataset, and the filter that scopes a simulation to it.
WALES = RegionResolution(
    code="country/wales",
    dataset_reference=NATIONAL_DATASET,
    scoping_strategy="households in Wales",
)


def _dataset(identity: str) -> Stage12Dataset:
    return Stage12Dataset(
        identity=identity,
        uri=f"hf://policyengine/microcosm-uk/{identity}.h5@{identity}-revision",
        artifact_revision=f"{identity}-revision",
        sha256="a" * 64,
        repo_type="model",
    )


def _route_uk_area_regions(monkeypatch, *, routed: bool = True) -> None:
    """Make the installed release route UK area regions to the local-area dataset."""
    monkeypatch.setattr(local_area_datasets, "uk_area_regions_routed", lambda: routed)
    monkeypatch.setattr(
        local_area_datasets, "routed_uk_area_dataset_name", lambda: LOCAL_AREA_DATASET
    )


def _installed_uk_bundle() -> ResolvedStage12Bundle:
    """Return an installed bundle holding the national and local-area datasets."""
    national = _dataset(NATIONAL_DATASET)
    region_datasets = {
        "national": NATIONAL_DATASET,
        "country": NATIONAL_DATASET,
        "constituency": LOCAL_AREA_DATASET,
        "local_authority": LOCAL_AREA_DATASET,
    }
    return ResolvedStage12Bundle(
        bundle=Stage12BundleManifest(
            policyengine_version=POLICYENGINE_VERSION,
            policyengine_requirement=f"policyengine=={POLICYENGINE_VERSION}",
            core_package_version="3.33.3",
            core_package_requirement="policyengine-core==3.33.3",
            countries=(
                Stage12CountryBundle(
                    country="uk",
                    country_package_name="policyengine-uk",
                    country_package_version=COUNTRY_PACKAGE_VERSION,
                    country_package_requirement=(
                        f"policyengine-uk=={COUNTRY_PACKAGE_VERSION}"
                    ),
                    data_package_name="microcosm-uk",
                    data_package_version="0.1.0",
                    data_release_version="release",
                    data_artifact_revision=national.artifact_revision,
                    default_dataset=NATIONAL_DATASET,
                    default_dataset_uri=national.uri,
                    datasets=(national, _dataset(LOCAL_AREA_DATASET)),
                    region_datasets=tuple(
                        Stage12RegionDataset(region_type=region_type, dataset=dataset)
                        for region_type, dataset in region_datasets.items()
                    ),
                ),
            ),
        ),
        bundle_manifest_sha256=BUNDLE_MANIFEST_SHA256,
    )


def _provenance(identity: str) -> BundleProvenance:
    dataset = _dataset(identity)
    return BundleProvenance(
        policyengine_version=POLICYENGINE_VERSION,
        country_package_name="policyengine-uk",
        country_package_version=COUNTRY_PACKAGE_VERSION,
        dataset=DatasetProvenance(
            identity=identity,
            uri=dataset.uri,
            artifact_revision=dataset.artifact_revision,
            data_package_name="microcosm-uk",
            data_package_version="0.1.0",
        ),
        bundle_manifest_sha256=BUNDLE_MANIFEST_SHA256,
    )


def _uk_simulation(
    region: str, identity: str, *, provenance: BundleProvenance | None = None
) -> PlannedSimulationExecutionInput:
    bundle = provenance or _provenance(identity)
    return PlannedSimulationExecutionInput(
        evaluation_id=EVALUATION_ID,
        simulation_execution_id=BASELINE_ID,
        role=SimulationRole.BASELINE,
        policy={},
        population=DatasetPopulationInput(
            artifact=DatasetArtifactReference(
                uri=bundle.dataset.uri,
                media_type=DatasetArtifactMediaType.HDF5,
                content_sha256="a" * 64,
            )
        ),
        year=2026,
        geography=GeographySelection(country="uk", region=region),
        bundle=bundle,
        output_plan=Stage12OutputPlan(
            country="uk",
            requirements=ReportOutputRequirements(
                aggregates=LOCAL_AREA_AGGREGATES,
                include_cliff_impacts=False,
                labor_supply_response_active=False,
            ),
            entities=(
                EntityOutputPlan(
                    entity="household",
                    materialized_variables=("household_id", "household_net_income"),
                ),
            ),
        ),
    )


@pytest.mark.parametrize("region", ["uk", "country/wales"])
def test_the_installed_bundle_accepts_the_local_area_dataset_for_uk_and_a_nation(
    monkeypatch, region: str
) -> None:
    _route_uk_area_regions(monkeypatch)
    monkeypatch.setattr(worker, "load_stage12_bundle", _installed_uk_bundle)

    worker._require_installed_bundle(_uk_simulation(region, LOCAL_AREA_DATASET))


def test_the_installed_bundle_refuses_the_dataset_when_the_release_does_not_route(
    monkeypatch,
) -> None:
    # On such a release a UK-wide run calculates the area results itself, as
    # Stage 12's national aggregation decides (uk_area_breakdowns_omitted).
    _route_uk_area_regions(monkeypatch, routed=False)
    monkeypatch.setattr(worker, "load_stage12_bundle", _installed_uk_bundle)

    with pytest.raises(RuntimeError, match="differs from the installed bundle"):
        worker._require_installed_bundle(_uk_simulation("uk", LOCAL_AREA_DATASET))


def test_the_installed_bundle_refuses_the_local_area_dataset_at_another_revision(
    monkeypatch,
) -> None:
    _route_uk_area_regions(monkeypatch)
    monkeypatch.setattr(worker, "load_stage12_bundle", _installed_uk_bundle)
    provenance = _provenance(LOCAL_AREA_DATASET)
    provenance = provenance.model_copy(
        update={
            "dataset": provenance.dataset.model_copy(
                update={"artifact_revision": "an-older-revision"}
            )
        }
    )

    with pytest.raises(RuntimeError, match="differs from the installed bundle"):
        worker._require_installed_bundle(
            _uk_simulation("uk", LOCAL_AREA_DATASET, provenance=provenance)
        )


class _StoppedBeforeSimulatingError(Exception):
    """Raised by a stubbed simulation build once the dataset has loaded."""


@dataclass
class _Calculation:
    """What a calculation stopped before simulating did."""

    selected_by: list[RegionResolution] = field(default_factory=list)
    built_with: list[Any] = field(default_factory=list)
    detected_on: list[str] = field(default_factory=list)


def _area_household() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "household_id": [1, 2],
            "constituency_code_oa": ["W07000081", "W07000082"],
            # Cumberland exists only in the LAD23 boundaries.
            "la_code_oa": ["E06000063", "W06000015"],
            "region_code_oa": ["W92000004", "W92000004"],
            "source_household_id": [10, 11],
        }
    )


def _run_until_built(
    monkeypatch,
    simulation: PlannedSimulationExecutionInput,
    *,
    household: pd.DataFrame,
) -> _Calculation:
    """Run a UK calculation of Wales up to its simulation build."""
    from policyengine_simulation_executor import simulation_runtime
    from policyengine_simulation_executor.stage12_cache import state_datasets

    calculation = _Calculation()
    dataset = SimpleNamespace(
        data=SimpleNamespace(entity_data={"household": household})
    )
    uris = {
        item.identity: item.uri
        for item in _installed_uk_bundle().bundle.countries[0].datasets
    }

    def select(_params, *, region_resolution: RegionResolution) -> DatasetSelection:
        calculation.selected_by.append(region_resolution)
        name = region_resolution.dataset_reference or NATIONAL_DATASET
        return DatasetSelection(
            name=name, uri=uris[name], is_default=name == NATIONAL_DATASET
        )

    def detect_on_the_national_dataset(_country, _dataset, **kwargs):
        calculation.detected_on.append(kwargs["dataset_name"])

    def stop_before_simulating(_params, *, resolved, **_kwargs):
        calculation.built_with.append(resolved)
        raise _StoppedBeforeSimulatingError

    monkeypatch.setattr(worker, "_require_installed_bundle", lambda _: None)
    _route_uk_area_regions(monkeypatch)
    monkeypatch.setattr(
        local_area_datasets, "load_stage12_bundle", _installed_uk_bundle
    )
    monkeypatch.setattr(simulation_runtime, "setup_gcp_credentials", nullcontext)
    monkeypatch.setattr(
        simulation_runtime, "_country_module", lambda _country: object()
    )
    monkeypatch.setattr(simulation_runtime, "resolve_region", lambda **_: WALES)
    monkeypatch.setattr(simulation_runtime, "_resolve_dataset_selection", select)
    monkeypatch.setattr(
        simulation_runtime, "_load_dataset", lambda *_args, **_kwargs: dataset
    )
    monkeypatch.setattr(
        state_datasets, "baked_state_data_folder", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        worker, "detect_uk_run_dataset_metadata", detect_on_the_national_dataset
    )
    monkeypatch.setattr(worker, "build_stage12_simulation", stop_before_simulating)

    with pytest.raises(_StoppedBeforeSimulatingError):
        worker.calculate_simulation_frames(simulation)
    return calculation


def test_a_local_area_run_loads_the_local_area_dataset_scoped_to_its_nation(
    monkeypatch,
) -> None:
    calculation = _run_until_built(
        monkeypatch,
        _uk_simulation("country/wales", LOCAL_AREA_DATASET),
        household=_area_household(),
    )

    (selected_by,) = calculation.selected_by
    assert selected_by.code == "country/wales"
    assert selected_by.dataset_reference == LOCAL_AREA_DATASET
    (built_with,) = calculation.built_with
    assert built_with.dataset_selection.name == LOCAL_AREA_DATASET
    # The simulation keeps Wales's own scoping on the local-area dataset.
    assert built_with.region == WALES
    # The area columns were checked on the local-area dataset as it loaded.
    assert calculation.detected_on == []


def test_a_national_run_of_a_nation_loads_the_dataset_its_region_names(
    monkeypatch,
) -> None:
    calculation = _run_until_built(
        monkeypatch,
        _uk_simulation("country/wales", NATIONAL_DATASET),
        household=_area_household(),
    )

    assert calculation.selected_by == [WALES]
    assert calculation.detected_on == [NATIONAL_DATASET]


def test_a_local_area_run_detects_the_authority_boundaries_of_its_dataset(
    monkeypatch,
) -> None:
    detected = []
    original = worker.detect_uk_area_dataset_metadata

    def record_detection(dataset, *, dataset_name):
        metadata = original(dataset, dataset_name=dataset_name)
        detected.append((dataset_name, metadata.boundary_version))
        return metadata

    monkeypatch.setattr(worker, "detect_uk_area_dataset_metadata", record_detection)

    _run_until_built(
        monkeypatch,
        _uk_simulation("uk", LOCAL_AREA_DATASET),
        household=_area_household(),
    )

    assert detected == [(LOCAL_AREA_DATASET, UKLocalAuthorityBoundaryVersion.LAD23)]


@pytest.mark.parametrize(
    "missing",
    ["constituency_code_oa", "la_code_oa", "region_code_oa", "source_household_id"],
)
def test_a_local_area_dataset_without_an_area_column_fails_before_simulating(
    monkeypatch, missing: str
) -> None:
    with pytest.raises(ValueError, match=f"has no {missing} column"):
        _run_until_built(
            monkeypatch,
            _uk_simulation("uk", LOCAL_AREA_DATASET),
            household=_area_household().drop(columns=[missing]),
        )
