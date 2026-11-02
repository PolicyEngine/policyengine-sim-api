"""Tests for selecting and loading a request's dataset."""

import json
from types import SimpleNamespace

import pytest
from policyengine_simulation_executor.release_bundle import (
    BUNDLE_RECEIPT_FILENAME,
    get_country_release_bundle,
)
from policyengine_simulation_executor.simulation_runtime import (
    DatasetSelection,
    RegionResolution,
    _nondefault_data_folder,
    _resolve_dataset_selection,
    resolve_dataset_reference,
)
from policyengine_simulation_executor.simulation_runtime import (
    _load_dataset as _load_selected_dataset,
)


def _load_dataset(params, *, country_module=None, region_resolution=None):
    """Exercise dataset resolution and loading together in direct loader tests."""
    selection = _resolve_dataset_selection(params, region_resolution=region_resolution)
    return _load_selected_dataset(
        params, country_module=country_module, selection=selection
    )


def test_resolve_dataset_reference_uses_bundle_default():
    bundle = get_country_release_bundle("us")

    assert resolve_dataset_reference("us") == bundle.default_dataset


def test_dataset_selection_identifies_the_actual_bundle_default():
    bundle = get_country_release_bundle("us")
    nondefault = next(
        name for name in bundle.dataset_uris if name != bundle.default_dataset
    )

    omitted = _resolve_dataset_selection({"country": "us"})
    alternate = _resolve_dataset_selection(
        {"country": "us"},
        region_resolution=RegionResolution(
            code="state/ca", dataset_reference=bundle.dataset_uris[nondefault]
        ),
    )

    assert isinstance(omitted, DatasetSelection)
    assert omitted.name == bundle.default_dataset
    assert omitted.is_default
    assert alternate.name == nondefault
    assert not alternate.is_default


def test_dataset_selection_rejects_a_bundled_uri_as_public_data():
    bundle = get_country_release_bundle("us")
    with pytest.raises(ValueError, match="Dataset overrides are not supported"):
        _resolve_dataset_selection(
            {"country": "us", "data": bundle.default_dataset_uri}
        )


@pytest.mark.parametrize("country", ["us", "uk"])
def test_load_dataset_delegates_default_selection_to_country_loader_with_receipt(
    country,
    tmp_path,
    monkeypatch,
):
    bundle = get_country_release_bundle(country)
    installed_dataset_path = tmp_path / f"{bundle.default_dataset}.h5"
    installed_dataset_path.write_bytes(b"data")
    receipt_path = tmp_path / BUNDLE_RECEIPT_FILENAME
    receipt_path.write_text(
        json.dumps(
            {
                "bundle_version": "4.18.3",
                "policyengine_version": "4.18.3",
                "datasets": [
                    {
                        "country": country,
                        "dataset": bundle.default_dataset,
                        "version": bundle.data_version,
                        "path": str(installed_dataset_path),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("POLICYENGINE_BUNDLE_RECEIPT", str(receipt_path))
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    get_country_release_bundle.cache_clear()

    dataset = SimpleNamespace()
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": dataset}

    assert (
        _load_dataset(
            {"country": country, "time_period": "2026"},
            country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        )
        is dataset
    )

    assert ensure_calls == [
        {
            "years": [2026],
            "data_folder": str(tmp_path),
        }
    ]


@pytest.mark.parametrize(
    "explicit_data_params",
    [
        {"data_version": "custom-v1"},
        {"data": "populace_us_2024@custom-v1"},
        {"data": "hf://other-org/other-repo/populace_us_2024.h5"},
    ],
)
def test_load_dataset_rejects_unmanaged_dataset_requests(
    explicit_data_params,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))

    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    with pytest.raises(ValueError, match="Dataset overrides are not supported"):
        _load_dataset(
            {"country": "us", "time_period": "2026", **explicit_data_params},
            country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        )

    assert ensure_calls == []


def test_load_dataset_rejects_explicit_default(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    with pytest.raises(ValueError, match="Dataset overrides are not supported"):
        _load_dataset(
            {"country": "us", "time_period": "2026", "data": "populace_us_2024"},
            country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        )

    assert ensure_calls == []


def test_load_dataset_passes_region_dataset_name_to_managed_loader(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    bundle = get_country_release_bundle("us")
    nondefault = next(
        name for name in bundle.dataset_uris if name != bundle.default_dataset
    )
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    _load_dataset(
        {"country": "us", "time_period": "2026"},
        country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        region_resolution=RegionResolution(
            code="state/ca", dataset_reference=bundle.dataset_uris[nondefault]
        ),
    )

    assert ensure_calls == [
        {
            "datasets": [nondefault],
            "years": [2026],
            "data_folder": _nondefault_data_folder(
                "us", nondefault, bundle.dataset_uris[nondefault]
            ),
        }
    ]


@pytest.mark.parametrize("use_default", [True, False])
def test_load_dataset_maps_region_bundle_uri_to_managed_name(
    use_default, tmp_path, monkeypatch
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    bundle = get_country_release_bundle("us")
    name = (
        bundle.default_dataset
        if use_default
        else next(
            name for name in bundle.dataset_uris if name != bundle.default_dataset
        )
    )
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    _load_dataset(
        {"country": "us", "time_period": "2026"},
        country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        region_resolution=SimpleNamespace(dataset_reference=bundle.dataset_uris[name]),
    )

    assert ensure_calls == [
        {
            **({} if use_default else {"datasets": [name]}),
            "years": [2026],
            "data_folder": (
                str(tmp_path)
                if use_default
                else _nondefault_data_folder("us", name, bundle.dataset_uris[name])
            ),
        }
    ]


def test_load_dataset_rejects_explicit_dataset_for_filtered_region(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    bundle = get_country_release_bundle("us")
    nondefault = next(
        name for name in bundle.dataset_uris if name != bundle.default_dataset
    )
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    with pytest.raises(ValueError, match="Dataset overrides are not supported"):
        _load_dataset(
            {"country": "us", "time_period": "2026", "data": nondefault},
            country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
            region_resolution=SimpleNamespace(
                dataset_reference=bundle.default_dataset_uri,
                scoping_strategy=object(),
            ),
        )

    assert ensure_calls == []


def test_nondefault_dataset_cache_is_distinct_for_shared_filename_stem():
    first = _nondefault_data_folder(
        "us", "certified_a", "hf://policyengine/a/shared.h5@revision-a"
    )
    second = _nondefault_data_folder(
        "us", "certified_b", "hf://policyengine/b/shared.h5@revision-b"
    )

    assert first != second


def test_load_dataset_reads_a_nondefault_dataset_from_the_folder_it_is_given(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path / "image-data"))
    bundle = get_country_release_bundle("us")
    nondefault = next(
        name for name in bundle.dataset_uris if name != bundle.default_dataset
    )
    selection = DatasetSelection(
        name=nondefault, uri=bundle.dataset_uris[nondefault], is_default=False
    )
    state_folder = str(tmp_path / "state-datasets" / nondefault / "UT")
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    _load_selected_dataset(
        {"country": "us", "time_period": "2026"},
        selection=selection,
        country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        data_folder=state_folder,
    )

    assert ensure_calls == [
        {"datasets": [nondefault], "years": [2026], "data_folder": state_folder}
    ]


def test_load_dataset_without_a_folder_keeps_the_temporary_nondefault_folder(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    bundle = get_country_release_bundle("us")
    nondefault = next(
        name for name in bundle.dataset_uris if name != bundle.default_dataset
    )
    selection = DatasetSelection(
        name=nondefault, uri=bundle.dataset_uris[nondefault], is_default=False
    )
    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    _load_selected_dataset(
        {"country": "us", "time_period": "2026"},
        selection=selection,
        country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        data_folder=None,
    )

    assert ensure_calls[0]["data_folder"] == _nondefault_data_folder(
        "us", nondefault, bundle.dataset_uris[nondefault]
    )


def test_load_dataset_uses_baked_folder_for_default_requests(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))

    ensure_calls = []

    def ensure_datasets(**kwargs):
        ensure_calls.append(kwargs)
        return {"dataset": SimpleNamespace()}

    _load_dataset(
        {"country": "us", "time_period": "2026"},
        country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
    )

    assert ensure_calls[0]["data_folder"] == str(tmp_path)
