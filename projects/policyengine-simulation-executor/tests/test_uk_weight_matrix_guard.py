"""Unit tests for binding UK weight-matrix regions to the certified release."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pandas as pd
import pytest
from policyengine.core.scoping_strategy import (
    RowFilterStrategy,
    WeightReplacementStrategy,
)

from policyengine_simulation_executor import simulation_output_geographic
from policyengine_simulation_executor import simulation_runtime as sr

_REPO = "policyengine/policyengine-uk-data-private"
_MATRIX = "parliamentary_constituency_weights.h5"
_STRATEGY = WeightReplacementStrategy(
    weight_matrix_bucket="policyengine-uk-data-private",
    weight_matrix_key=_MATRIX,
    lookup_csv_bucket="policyengine-uk-data-private",
    lookup_csv_key="constituencies_2024.csv",
    region_code="E14001063",
    download_missing_assets=False,
)
_SELECTION = SimpleNamespace(name="enhanced_frs_2024_25")


def _reference(path: str, revision: str = "1.56.16") -> SimpleNamespace:
    return SimpleNamespace(path=path, repo_id=_REPO, revision=revision)


def _bundle(monkeypatch, *, matrix_revision: str = "1.56.16") -> None:
    manifest = SimpleNamespace(
        data_package=SimpleNamespace(
            repo_id=_REPO, version="1.56.16", release_manifest_revision=None
        ),
        datasets={
            "enhanced_frs_2024_25": _reference("enhanced_frs_2024_25.h5"),
            "parliamentary_constituency_weights": _reference(_MATRIX, matrix_revision),
        },
    )
    monkeypatch.setattr(
        "policyengine.provenance.manifest.get_release_manifest",
        lambda country: manifest,
    )


def _write_matrix(path: Path, households: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as matrix:
        matrix.create_dataset("2025", data=np.ones((2, households)))


def _assets(monkeypatch, tmp_path, *, households: int = 3, certified_dir=None):
    """Search UK geography assets in tmp_path and stub the certified download."""

    monkeypatch.setenv("POLICYENGINE_UK_GEOGRAPHY_DATA_DIR", str(tmp_path))
    calls: list = []

    def materialize(country, dataset, *, data_dir, **kwargs):
        target = Path(certified_dir or data_dir) / _MATRIX
        _write_matrix(target, households)
        calls.append((country, dataset, Path(data_dir)))
        return SimpleNamespace(path=str(target))

    def lookup(spec):
        path = tmp_path / spec.lookup_csv_filename
        path.write_text("code,name,x,y\nE14001063,Aldershot,56,-40\n")
        return str(path)

    monkeypatch.setattr(
        "policyengine.provenance.dataset_materialization.materialize_dataset",
        materialize,
    )
    monkeypatch.setattr(
        simulation_output_geographic, "_required_uk_geography_lookup_csv_path", lookup
    )
    return calls


def _dataset(households: int, year: int = 2025) -> SimpleNamespace:
    household = pd.DataFrame({"household_id": range(1, households + 1)})
    return SimpleNamespace(
        year=year,
        data=SimpleNamespace(entity_data={"household": household}),
    )


def test_fallback_regions_never_download_the_bucket_matrix() -> None:
    region = sr._build_uk_weight_replacement_region("constituency/E14001063")

    assert region is not None
    assert region.scoping_strategy.download_missing_assets is False


@pytest.mark.parametrize(
    "strategy",
    [None, RowFilterStrategy(variable_name="country", variable_value="ENGLAND")],
)
def test_guard_ignores_regions_without_weight_matrices(monkeypatch, strategy) -> None:
    monkeypatch.setattr(
        "policyengine.provenance.manifest.get_release_manifest",
        lambda country: pytest.fail("no weight matrix should be resolved"),
    )

    sr._require_certified_uk_weight_matrix(strategy, _dataset(3), _SELECTION)


def test_guard_reads_the_matrix_certified_with_the_selected_dataset(
    monkeypatch, tmp_path
) -> None:
    _bundle(monkeypatch)
    calls = _assets(monkeypatch, tmp_path)

    sr._require_certified_uk_weight_matrix(_STRATEGY, _dataset(3), _SELECTION)

    assert calls == [("uk", "parliamentary_constituency_weights", tmp_path)]


def test_guard_rejects_a_matrix_from_another_release(monkeypatch, tmp_path) -> None:
    _bundle(monkeypatch, matrix_revision="1.57.4")
    _assets(monkeypatch, tmp_path)

    with pytest.raises(
        ValueError,
        match=(
            r"has no parliamentary_constituency_weights\.h5 from the release of "
            r"'enhanced_frs_2024_25'"
        ),
    ):
        sr._require_certified_uk_weight_matrix(_STRATEGY, _dataset(3), _SELECTION)


def test_guard_rejects_a_dataset_outside_the_bundle(monkeypatch, tmp_path) -> None:
    _bundle(monkeypatch)
    _assets(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="is not in the certified bundle"):
        sr._require_certified_uk_weight_matrix(
            _STRATEGY, _dataset(3), SimpleNamespace(name="populace_uk_2023")
        )


def test_guard_rejects_a_copy_that_shadows_the_certified_matrix(
    monkeypatch, tmp_path
) -> None:
    _bundle(monkeypatch)
    _assets(monkeypatch, tmp_path, certified_dir=tmp_path / "certified")
    _write_matrix(tmp_path / _MATRIX, households=3)

    with pytest.raises(ValueError, match="would read .*, not the parliamentary"):
        sr._require_certified_uk_weight_matrix(_STRATEGY, _dataset(3), _SELECTION)


def test_guard_rejects_a_dataset_of_another_size(monkeypatch, tmp_path) -> None:
    _bundle(monkeypatch)
    _assets(monkeypatch, tmp_path, households=3)

    with pytest.raises(
        ValueError,
        match=(r"which was built for 3 households; the selected UK dataset has 4"),
    ):
        sr._require_certified_uk_weight_matrix(_STRATEGY, _dataset(4), _SELECTION)


def test_guard_rejects_a_year_the_matrix_does_not_cover(monkeypatch, tmp_path) -> None:
    _bundle(monkeypatch)
    _assets(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match=r"has no weights for 2026 \(it covers 2025\)"):
        sr._require_certified_uk_weight_matrix(_STRATEGY, _dataset(3, 2026), _SELECTION)
