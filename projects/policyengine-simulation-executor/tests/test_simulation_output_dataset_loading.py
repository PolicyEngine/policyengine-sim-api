"""Tests for the dataset a runtime run loads, and the data version it reports."""

from types import SimpleNamespace

import pytest
from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from fixtures.spm_doubles import installed_spm_selection, spm_capable_simulation
from policyengine_simulation_executor.release_bundle import get_country_release_bundle
from policyengine_simulation_executor.simulation_runtime import (
    DatasetSelection,
    RegionResolution,
    _run_simulation_impl_core,
    resolve_data_folder,
)
from test_simulation_output_fakes import TrackingRuntime


class _StoppedAfterLoadingError(Exception):
    """Raised by a stubbed simulation build once the dataset has loaded."""


def _load_through_runtime(
    monkeypatch, *, region_resolution, selection, baked_folder, time_period=2026
):
    """Run the annual runtime up to its dataset load and return what it did."""
    from policyengine_simulation_executor import state_datasets

    load_calls = []
    lookups = []

    def fake_load_dataset(_params, *, country_module, selection, data_folder=None):
        load_calls.append({"selection": selection, "data_folder": data_folder})
        return object()

    def fake_baked_state_data_folder(**kwargs):
        lookups.append(kwargs)
        return baked_folder

    def stop_after_loading(*_args, **_kwargs):
        raise _StoppedAfterLoadingError

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._country_module",
        lambda _country: SimpleNamespace(model=SimpleNamespace(version="2.2.1")),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.resolve_region",
        lambda **_kwargs: region_resolution,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_dataset_selection",
        lambda _params, *, region_resolution: selection,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._load_dataset",
        fake_load_dataset,
    )
    monkeypatch.setattr(
        state_datasets, "baked_state_data_folder", fake_baked_state_data_folder
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._build_simulation",
        stop_after_loading,
    )
    runtime = TrackingRuntime()
    with pytest.raises(_StoppedAfterLoadingError):
        _run_simulation_impl_core(
            {
                "country": "us",
                "region": region_resolution.code,
                "time_period": time_period,
                "baseline": {},
                "reform": {},
            },
            runtime=runtime,
        )
    return SimpleNamespace(
        load_calls=load_calls, lookups=lookups, context=dict(runtime.context)
    )


def _regional_selection():
    return DatasetSelection(
        name="populace_us_2024_acs_local",
        uri="hf://policyengine/populace-us/populace_us_2024_acs_local.h5@rev",
        is_default=False,
    )


def test_runtime_loads_a_state_from_its_baked_folder(monkeypatch):
    loaded = _load_through_runtime(
        monkeypatch,
        region_resolution=RegionResolution(
            code="state/ut", dataset_reference="ref", state_code="UT"
        ),
        selection=_regional_selection(),
        baked_folder="/opt/policyengine/data/state-datasets/acs/UT",
    )

    assert loaded.load_calls == [
        {
            "selection": _regional_selection(),
            "data_folder": "/opt/policyengine/data/state-datasets/acs/UT",
        }
    ]
    assert loaded.lookups == [
        {
            "country": "us",
            "selection": _regional_selection(),
            "state_code": "UT",
            "year": 2026,
            "data_folder": resolve_data_folder(),
        }
    ]
    assert loaded.context["dataset_source"] == "baked_state"


def test_runtime_prepares_a_state_on_demand_without_a_baked_file(monkeypatch):
    loaded = _load_through_runtime(
        monkeypatch,
        region_resolution=RegionResolution(
            code="congressional_district/UT-01",
            dataset_reference="ref",
            state_code="UT",
        ),
        selection=_regional_selection(),
        baked_folder=None,
        time_period=2028,
    )

    assert loaded.load_calls[0]["data_folder"] is None
    assert loaded.lookups[0]["year"] == 2028
    assert loaded.context["dataset_source"] == "on_demand"


def test_runtime_loads_a_national_run_from_the_default_folder(monkeypatch):
    bundle = get_country_release_bundle("us")
    loaded = _load_through_runtime(
        monkeypatch,
        region_resolution=RegionResolution(code="us"),
        selection=DatasetSelection(
            name=bundle.default_dataset,
            uri=bundle.default_dataset_uri,
            is_default=True,
        ),
        baked_folder=None,
    )

    assert loaded.load_calls[0]["data_folder"] is None
    assert "dataset_source" not in loaded.context


def _builder_data_version(monkeypatch, *, region_resolution, dataset_selection):
    """Run the annual runtime and return the data version it gives the builder."""
    from policyengine_simulation_executor import state_datasets

    selection = installed_spm_selection()
    simulations = [spm_capable_simulation(selection), spm_capable_simulation(selection)]
    builder_calls = []

    class FakeSimulationOutputBuilder:
        def __init__(self, **kwargs):
            builder_calls.append(kwargs)

        def serialize(self):
            return dict(CURRENT_SINGLE_YEAR_MACRO_RESULT)

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._country_module",
        lambda _country: SimpleNamespace(model=SimpleNamespace(version="2.2.1")),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.resolve_region",
        lambda **_kwargs: region_resolution,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._resolve_dataset_selection",
        lambda _params, *, region_resolution: dataset_selection,
    )
    monkeypatch.setattr(
        state_datasets, "baked_state_data_folder", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._load_dataset",
        lambda _params, country_module, selection, data_folder=None: object(),
    )
    build_iter = iter(simulations)
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._build_simulation",
        lambda *_args, **_kwargs: next(build_iter),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.SimulationOutputBuilder",
        FakeSimulationOutputBuilder,
    )

    _run_simulation_impl_core(
        {
            "country": "us",
            "region": region_resolution.code,
            "baseline": {},
            "reform": {},
        },
        runtime=TrackingRuntime(),
    )
    return builder_calls[0]["resolved_data_version"]


def test_a_state_run_reports_the_revision_of_the_dataset_it_loads(monkeypatch):
    from policyengine.provenance.manifest import get_release_manifest

    bundle = get_country_release_bundle("us")
    regional = "populace_us_2024_acs_local"

    data_version = _builder_data_version(
        monkeypatch,
        region_resolution=RegionResolution(
            code="state/ut", dataset_reference="ref", state_code="UT"
        ),
        dataset_selection=DatasetSelection(
            name=regional, uri=bundle.dataset_uris[regional], is_default=False
        ),
    )

    assert data_version == get_release_manifest("us").datasets[regional].revision
    assert data_version != bundle.data_version


def test_a_national_run_keeps_the_release_data_version(monkeypatch):
    bundle = get_country_release_bundle("us")

    data_version = _builder_data_version(
        monkeypatch,
        region_resolution=RegionResolution(code="us"),
        dataset_selection=DatasetSelection(
            name=bundle.default_dataset,
            uri=bundle.default_dataset_uri,
            is_default=True,
        ),
    )

    assert data_version is None
