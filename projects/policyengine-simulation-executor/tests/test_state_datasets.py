"""The main path's baked per-state datasets: their folders and the runtime lookup."""

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from policyengine_simulation_executor import state_datasets
from policyengine_simulation_executor.release_bundle import get_country_release_bundle
from policyengine_simulation_executor.simulation_runtime import DatasetSelection


def test_state_dataset_relative_folder_names_the_dataset_and_the_state():
    assert (
        state_datasets.state_dataset_relative_folder("populace_us_2024_acs_local", "ut")
        == "state-datasets/populace_us_2024_acs_local/UT"
    )


def test_state_dataset_folder_sits_under_the_data_folder():
    assert (
        state_datasets.state_dataset_folder(
            "/opt/policyengine/data", "populace_us_2024_acs_local", "UT"
        )
        == "/opt/policyengine/data/state-datasets/populace_us_2024_acs_local/UT"
    )


@pytest.fixture
def regional_selection():
    from fixtures.state_dataset_sources import REGIONAL_DATASET

    bundle = get_country_release_bundle("us")
    return DatasetSelection(
        name=REGIONAL_DATASET,
        uri=bundle.dataset_uris[REGIONAL_DATASET],
        is_default=False,
    )


@pytest.fixture
def baked_utah(tmp_path):
    from fixtures.state_dataset_sources import REGIONAL_DATASET, write_year_file

    folder = Path(
        state_datasets.state_dataset_folder(str(tmp_path), REGIONAL_DATASET, "UT")
    )
    folder.mkdir(parents=True)
    return SimpleNamespace(
        data_folder=str(tmp_path),
        folder=folder,
        year_file=write_year_file(folder, year=2026),
    )


def _lookup(baked, selection, *, state_code="UT", year=2026, country="us"):
    return state_datasets.baked_state_data_folder(
        country=country,
        selection=selection,
        state_code=state_code,
        year=year,
        data_folder=baked.data_folder,
    )


def test_a_state_reads_its_baked_folder(baked_utah, regional_selection):
    assert _lookup(baked_utah, regional_selection) == str(baked_utah.folder)


def test_a_year_without_a_baked_file_reads_nothing_baked(
    baked_utah, regional_selection
):
    assert _lookup(baked_utah, regional_selection, year=2028) is None


def test_a_state_without_a_baked_folder_reads_nothing_baked(
    baked_utah, regional_selection
):
    assert _lookup(baked_utah, regional_selection, state_code="WY") is None


def test_an_unstamped_file_reads_nothing_baked(tmp_path, regional_selection):
    from fixtures.state_dataset_sources import REGIONAL_DATASET, write_year_file

    folder = Path(
        state_datasets.state_dataset_folder(str(tmp_path), REGIONAL_DATASET, "UT")
    )
    folder.mkdir(parents=True)
    write_year_file(folder, year=2026, stamp=False)

    assert (
        state_datasets.baked_state_data_folder(
            country="us",
            selection=regional_selection,
            state_code="UT",
            year=2026,
            data_folder=str(tmp_path),
        )
        is None
    )


def test_a_file_with_another_spm_profile_reads_nothing_baked(
    baked_utah, regional_selection
):
    from policyengine.tax_benefit_models.us.legacy_spm.record import (
        LegacySPMProfile,
        LegacySPMRecord,
        read_record_value,
        write_record,
    )

    stored = LegacySPMRecord.model_validate(read_record_value(baked_utah.year_file))
    write_record(
        baked_utah.year_file,
        LegacySPMRecord(
            profile=LegacySPMProfile(
                adapter_revision=stored.profile.adapter_revision + 1,
                source=stored.profile.source,
            )
        ),
    )

    assert _lookup(baked_utah, regional_selection) is None


def test_the_default_dataset_reads_nothing_baked(baked_utah):
    bundle = get_country_release_bundle("us")
    default = DatasetSelection(
        name=bundle.default_dataset, uri=bundle.default_dataset_uri, is_default=True
    )

    assert _lookup(baked_utah, default) is None


def test_a_region_without_a_state_reads_nothing_baked(baked_utah, regional_selection):
    assert _lookup(baked_utah, regional_selection, state_code=None) is None
    assert _lookup(baked_utah, regional_selection, country="uk") is None


def test_a_district_request_loads_its_states_baked_file(
    monkeypatch, baked_utah, regional_selection
):
    """A district resolves to its state's code and reads the state's file."""
    from policyengine.core.scoping_strategy import RowFilterStrategy
    from policyengine_simulation_executor import simulation_runtime

    district = SimpleNamespace(
        dataset_path=regional_selection.uri,
        scoping_strategy=RowFilterStrategy(
            variable_name="congressional_district_geoid", variable_value=4901
        ),
        parent_code="state/ut",
        state_code="UT",
    )
    country_module = SimpleNamespace(
        model=SimpleNamespace(
            version="2.2.1",
            get_region={"congressional_district/UT-01": district}.get,
        )
    )
    loads = []

    class _StoppedAfterLoadingError(Exception):
        pass

    def load_dataset(_params, *, country_module, selection, data_folder=None):
        loads.append(data_folder)
        raise _StoppedAfterLoadingError

    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", baked_utah.data_folder)
    monkeypatch.setattr(
        simulation_runtime, "_country_module", lambda _country: country_module
    )
    monkeypatch.setattr(simulation_runtime, "_load_dataset", load_dataset)

    with pytest.raises(_StoppedAfterLoadingError):
        simulation_runtime._run_simulation_impl_core(
            {
                "country": "us",
                "region": "congressional_district/UT-01",
                "time_period": 2026,
                "baseline": {},
                "reform": {},
            },
            runtime=SimpleNamespace(
                span=lambda *_args, **_kwargs: contextlib.nullcontext(),
                set_context=lambda **_attributes: None,
            ),
        )

    assert loads == [str(baked_utah.folder)]


def test_a_renamed_dotpy_reuse_check_falls_back_to_on_demand_loading(
    monkeypatch, baked_utah, regional_selection
):
    """A .py upgrade moving the private SPM profile helper must not fail requests."""
    from policyengine.tax_benefit_models.us import datasets as us_datasets

    monkeypatch.delattr(us_datasets, "_expected_legacy_spm_profile")

    assert _lookup(baked_utah, regional_selection) is None
