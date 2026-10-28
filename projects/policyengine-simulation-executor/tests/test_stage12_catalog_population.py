"""A v2 economy run scopes its dataset with its catalog population's filters."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from policyengine.core.scoping_strategy import RegionGroupStrategy, RowFilterStrategy
from policyengine_simulation_contract.economy_runs import (
    CatalogPopulation,
    CatalogRowFilter,
)
from policyengine_simulation_contract.stage12_bundle import (
    ResolvedStage12Bundle,
    Stage12BundleManifest,
    Stage12CountryBundle,
    Stage12Dataset,
)
from policyengine_simulation_contract.stage12_execution import (
    BundleProvenance,
    DatasetArtifactMediaType,
    DatasetArtifactReference,
    DatasetPopulationInput,
    DatasetProvenance,
    EntityOutputPlan,
    GeographySelection,
    PlannedSimulationExecutionInput,
    ReportAggregate,
    ReportOutputRequirements,
    SimulationRole,
    Stage12OutputPlan,
)
from policyengine_simulation_executor import simulation_runtime
from policyengine_simulation_executor.stage12_cache import state_datasets
from policyengine_simulation_executor.stage12_runtime import fanout
from policyengine_simulation_executor.stage12_runtime import simulation as worker
from policyengine_simulation_executor.stage12_runtime.catalog_population import (
    CATALOG_POPULATION_OPTION,
    catalog_region_groups,
)
from policyengine_simulation_executor.stage12_runtime.partition import US_REGION_GROUPS

NATIONAL = Stage12Dataset(
    identity="populace_us_2024",
    uri="hf://policyengine/data/populace_us_2024.h5@revision",
    artifact_revision="revision",
    sha256="a" * 64,
    repo_type="dataset",
)
STATES = Stage12Dataset(
    identity="populace_us_2024_acs_local",
    uri="hf://policyengine/data/populace_us_2024_acs_local.h5@states",
    artifact_revision="states",
    sha256="c" * 64,
    repo_type="dataset",
)
STATE_FILTERS = {
    code: CatalogRowFilter(field="state_fips", value=str(index + 1))
    for index, code in enumerate(
        sorted({code for group in US_REGION_GROUPS for code in group})
    )
}


class _StoppedAfterLoadingError(Exception):
    pass


def _installed_bundle() -> ResolvedStage12Bundle:
    return ResolvedStage12Bundle(
        bundle=Stage12BundleManifest(
            policyengine_version="6.7.5",
            policyengine_requirement="policyengine==6.7.5",
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
                    default_dataset=NATIONAL.identity,
                    default_dataset_uri=NATIONAL.uri,
                    datasets=(NATIONAL, STATES),
                ),
            ),
        ),
        bundle_manifest_sha256="b" * 64,
    )


def _provenance(dataset: Stage12Dataset) -> BundleProvenance:
    return BundleProvenance(
        policyengine_version="6.7.5",
        country_package_name="policyengine-us",
        country_package_version="1.764.6",
        dataset=DatasetProvenance(
            identity=dataset.identity,
            uri=dataset.uri,
            artifact_revision=dataset.artifact_revision,
            data_package_name="populace-data",
            data_package_version="0.1.0",
        ),
        bundle_manifest_sha256="b" * 64,
    )


def _planned(
    population: CatalogPopulation,
    dataset: Stage12Dataset,
    *,
    region_group: list[str] | None = None,
) -> PlannedSimulationExecutionInput:
    options: dict = {CATALOG_POPULATION_OPTION: population.model_dump(mode="json")}
    if region_group is not None:
        options["region_group"] = region_group
    return PlannedSimulationExecutionInput(
        evaluation_id="00000000-0000-0000-0000-000000000001",
        simulation_execution_id="00000000-0000-0000-0000-000000000001",
        role=SimulationRole.BASELINE,
        policy={},
        population=DatasetPopulationInput(
            artifact=DatasetArtifactReference(
                uri=dataset.uri,
                media_type=DatasetArtifactMediaType.HDF5,
                content_sha256=dataset.sha256,
            )
        ),
        year=2026,
        geography=GeographySelection(country="us", region=population.region_code),
        options=options,
        bundle=_provenance(dataset),
        output_plan=Stage12OutputPlan(
            country="us",
            requirements=ReportOutputRequirements(
                aggregates=tuple(ReportAggregate),
                include_cliff_impacts=False,
                labor_supply_response_active=False,
            ),
            entities=(
                EntityOutputPlan(
                    entity="household",
                    materialized_variables=("household_id",),
                ),
            ),
        ),
    )


def _state_population() -> CatalogPopulation:
    return CatalogPopulation(
        region_code="state/ca",
        dataset_name=STATES.identity,
        row_filter=CatalogRowFilter(field="state_fips", value="6"),
        state_code="CA",
    )


def _national_population() -> CatalogPopulation:
    return CatalogPopulation(
        region_code="us",
        dataset_name=NATIONAL.identity,
        state_filters=STATE_FILTERS,
    )


def _calculate_until_built(
    monkeypatch: pytest.MonkeyPatch, planned: PlannedSimulationExecutionInput
) -> SimpleNamespace:
    """Run the calculation up to building the simulation; return what it used."""
    loads: list = []
    built: list = []

    def refuse_registry(*_args, **_kwargs):
        pytest.fail("the region registry was consulted")

    def load_dataset(_params, *, selection, country_module, data_folder=None):
        loads.append({"selection": selection, "data_folder": data_folder})
        return SimpleNamespace(data=SimpleNamespace(entity_data={}))

    def stop_after_building(_params, *, resolved, policy, execution):
        built.append(resolved)
        raise _StoppedAfterLoadingError

    monkeypatch.setattr(worker, "load_stage12_bundle", _installed_bundle)
    monkeypatch.setattr(simulation_runtime, "setup_gcp_credentials", nullcontext)
    monkeypatch.setattr(simulation_runtime, "_country_module", lambda _: object())
    monkeypatch.setattr(simulation_runtime, "resolve_region", refuse_registry)
    monkeypatch.setattr(
        simulation_runtime, "_resolve_dataset_selection", refuse_registry
    )
    monkeypatch.setattr(simulation_runtime, "_load_dataset", load_dataset)
    monkeypatch.setattr(
        state_datasets, "baked_state_data_folder", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        worker, "detect_uk_run_dataset_metadata", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(worker, "build_stage12_simulation", stop_after_building)
    with pytest.raises(_StoppedAfterLoadingError):
        worker.calculate_simulation_frames(planned)
    return SimpleNamespace(loads=loads, resolved=built[0])


def test_a_catalog_state_run_filters_on_the_catalog_value_without_the_registry(
    monkeypatch,
) -> None:
    used = _calculate_until_built(monkeypatch, _planned(_state_population(), STATES))

    assert used.resolved.region.code == "state/ca"
    assert used.resolved.region.state_code == "CA"
    assert used.resolved.region.scoping_strategy == RowFilterStrategy(
        variable_name="state_fips", variable_value=6
    )
    assert used.loads[0]["selection"].name == STATES.identity
    assert used.loads[0]["selection"].is_default is False


def test_a_catalog_national_run_simulates_the_whole_named_dataset(
    monkeypatch,
) -> None:
    used = _calculate_until_built(
        monkeypatch, _planned(_national_population(), NATIONAL)
    )

    assert used.resolved.region.scoping_strategy is None
    assert used.loads[0]["selection"].name == NATIONAL.identity
    assert used.loads[0]["selection"].is_default is True


def test_a_catalog_segment_is_scoped_to_its_states_catalog_filters(
    monkeypatch,
) -> None:
    used = _calculate_until_built(
        monkeypatch,
        _planned(
            _national_population(), NATIONAL, region_group=["state/ny", "state/ca"]
        ),
    )

    strategy = used.resolved.region.scoping_strategy
    assert isinstance(strategy, RegionGroupStrategy)
    assert strategy.members == [
        RowFilterStrategy(
            variable_name="state_fips",
            variable_value=STATE_FILTERS["state/ny"].household_value(),
        ),
        RowFilterStrategy(
            variable_name="state_fips",
            variable_value=STATE_FILTERS["state/ca"].household_value(),
        ),
    ]


def test_a_catalog_segment_with_a_state_the_catalog_lacks_is_refused(
    monkeypatch,
) -> None:
    population = _national_population().model_copy(
        update={"state_filters": {"state/ca": STATE_FILTERS["state/ca"]}}
    )

    with pytest.raises(ValueError, match="no filter for state/ny"):
        _calculate_until_built(
            monkeypatch,
            _planned(population, NATIONAL, region_group=["state/ny", "state/ca"]),
        )


def test_a_catalog_dataset_the_installed_release_lacks_is_refused(
    monkeypatch,
) -> None:
    population = _state_population().model_copy(
        update={"dataset_name": "populace_us_2023"}
    )

    with pytest.raises(ValueError, match="no dataset 'populace_us_2023'"):
        _calculate_until_built(monkeypatch, _planned(population, STATES))


def test_the_installed_bundle_accepts_the_dataset_the_catalog_names(
    monkeypatch,
) -> None:
    monkeypatch.setattr(worker, "load_stage12_bundle", _installed_bundle)
    national_on_states = _national_population().model_copy(
        update={"dataset_name": STATES.identity}
    )

    worker._require_installed_bundle(_planned(national_on_states, STATES))


def test_the_installed_bundle_refuses_provenance_of_another_dataset(
    monkeypatch,
) -> None:
    monkeypatch.setattr(worker, "load_stage12_bundle", _installed_bundle)
    planned = _planned(_state_population(), STATES).model_copy(
        update={"bundle": _provenance(NATIONAL)}
    )

    with pytest.raises(RuntimeError, match="differs from the installed bundle"):
        worker._require_installed_bundle(planned)


def test_catalog_segments_cover_every_catalog_state() -> None:
    extra = CatalogRowFilter(field="state_fips", value="72")
    population = _national_population().model_copy(
        update={"state_filters": {**STATE_FILTERS, "state/pr": extra}}
    )

    groups = catalog_region_groups(population)

    assert len(groups) == len(US_REGION_GROUPS)
    assert groups[-1][-1] == "state/pr"
    assert {code for group in groups for code in group} == set(
        population.state_filters or {}
    )


def test_catalog_segments_refuse_a_partition_state_the_catalog_lacks() -> None:
    filters = dict(STATE_FILTERS)
    del filters["state/tx"]
    population = _national_population().model_copy(update={"state_filters": filters})

    with pytest.raises(ValueError, match="no filter for state/tx"):
        catalog_region_groups(population)


def test_a_catalog_segmented_run_uses_the_catalog_states(monkeypatch) -> None:
    runner = fanout.Stage12SegmentRunner(
        _planned(_national_population(), NATIONAL),
        app_name="policyengine-simulation-v2-6-7-5",
        runtime=SimpleNamespace(),
        seams=fanout.SegmentRunnerSeams(
            modal_module=SimpleNamespace(
                Function=SimpleNamespace(from_name=lambda *_args: object())
            )
        ),
    )
    monkeypatch.setattr(
        fanout,
        "_country_module",
        lambda _country: pytest.fail("the region registry was consulted"),
    )

    assert runner._groups() == catalog_region_groups(_national_population())
