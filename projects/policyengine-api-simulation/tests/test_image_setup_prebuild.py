"""Tests for the TEMPORARY single-year dataset prebuild (see issue #596).

These pin the coupling between the image-build prebuild and the runtime
dataset lookup: both must resolve the same ``{stem}_year_{year}.h5`` cache
paths, otherwise the baked files are silently ignored and every cold
container falls back to the slow runtime build.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from policyengine.provenance.manifest import (
    dataset_logical_name,
    get_release_manifest,
    resolve_dataset_reference,
)

from policyengine_api_simulation.release_bundle import (
    get_country_release_bundle,
    resolve_bundle_dataset_name,
)
from policyengine_api_simulation.simulation_runtime import DEFAULT_YEAR
from src.modal._image_setup import (
    PREBUILD_DATASET_YEARS,
    prebuild_country_datasets,
)


def _expected_stem(country: str) -> str:
    default_dataset = get_release_manifest(country).default_dataset
    return dataset_logical_name(resolve_dataset_reference(country, default_dataset))


def _install_country_stub(monkeypatch, country: str, ensure_datasets):
    monkeypatch.setitem(
        sys.modules,
        f"policyengine.tax_benefit_models.{country}",
        SimpleNamespace(ensure_datasets=ensure_datasets),
    )


@pytest.mark.parametrize("country", ["us", "uk"])
def test_prebuild_stem_matches_runtime_default_lookup(country, monkeypatch):
    """The critical coupling pin: the stem the prebuild writes must equal the
    stem the runtime resolves for default requests. If bundle metadata ever
    diverges from the packaged manifest default, the baked files would be
    silently unused — this test turns that into a CI failure on every
    policyengine version bump."""
    monkeypatch.delenv("POLICYENGINE_BUNDLE_RECEIPT", raising=False)
    get_country_release_bundle.cache_clear()

    runtime_stem = dataset_logical_name(
        resolve_dataset_reference(country, resolve_bundle_dataset_name(country, None))
    )

    assert _expected_stem(country) == runtime_stem


def test_prebuild_years_cover_runtime_default_year():
    assert PREBUILD_DATASET_YEARS == [2025, 2026, 2027]
    assert DEFAULT_YEAR in PREBUILD_DATASET_YEARS


@pytest.mark.parametrize("country", ["us", "uk"])
def test_prebuild_passes_explicit_manifest_default(country, tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    stem = _expected_stem(country)
    calls = []

    def ensure_datasets(**kwargs):
        calls.append(kwargs)
        for year in kwargs["years"]:
            Path(kwargs["data_folder"], f"{stem}_year_{year}.h5").write_bytes(b"h5")
        return {}

    _install_country_stub(monkeypatch, country, ensure_datasets)

    prebuild_country_datasets(country)

    assert calls == [
        {
            "datasets": [get_release_manifest(country).default_dataset],
            "years": [2025, 2026, 2027],
            "data_folder": str(tmp_path),
        }
    ]


def test_prebuild_skips_years_that_already_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    stem = _expected_stem("us")
    (tmp_path / f"{stem}_year_2025.h5").write_bytes(b"h5")
    calls = []

    def ensure_datasets(**kwargs):
        calls.append(kwargs)
        for year in kwargs["years"]:
            Path(kwargs["data_folder"], f"{stem}_year_{year}.h5").write_bytes(b"h5")
        return {}

    _install_country_stub(monkeypatch, "us", ensure_datasets)

    prebuild_country_datasets("us")

    assert calls == [
        {
            "datasets": [get_release_manifest("us").default_dataset],
            "years": [2026, 2027],
            "data_folder": str(tmp_path),
        }
    ]


def test_prebuild_skips_entirely_when_all_years_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    stem = _expected_stem("us")
    for year in PREBUILD_DATASET_YEARS:
        (tmp_path / f"{stem}_year_{year}.h5").write_bytes(b"h5")
    calls = []

    def ensure_datasets(**kwargs):
        calls.append(kwargs)
        return {}

    _install_country_stub(monkeypatch, "us", ensure_datasets)

    prebuild_country_datasets("us")

    assert calls == []


def test_prebuild_raises_when_files_not_produced(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))

    def ensure_datasets(**kwargs):
        return {}

    _install_country_stub(monkeypatch, "us", ensure_datasets)

    with pytest.raises(RuntimeError, match="did not produce"):
        prebuild_country_datasets("us")
