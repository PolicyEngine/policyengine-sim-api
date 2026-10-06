"""Unit tests for the UK weight-matrix fallback guard."""

from __future__ import annotations

from types import SimpleNamespace

import h5py
import numpy as np
import pandas as pd
import pytest
from policyengine.core.scoping_strategy import (
    RowFilterStrategy,
    WeightReplacementStrategy,
)

from policyengine_simulation_executor import simulation_runtime as sr

_STRATEGY = WeightReplacementStrategy(
    weight_matrix_bucket="policyengine-uk-data-private",
    weight_matrix_key="parliamentary_constituency_weights.h5",
    lookup_csv_bucket="policyengine-uk-data-private",
    lookup_csv_key="constituencies_2024.csv",
    region_code="E14001063",
)


def _dataset(households: int, year: int = 2025) -> SimpleNamespace:
    household = pd.DataFrame({"household_id": range(1, households + 1)})
    return SimpleNamespace(
        year=year,
        data=SimpleNamespace(entity_data={"household": household}),
    )


def _matrix(tmp_path, households: int) -> str:
    path = tmp_path / "parliamentary_constituency_weights.h5"
    with h5py.File(path, "w") as matrix:
        matrix.create_dataset("2025", data=np.ones((2, households)))
    return str(path)


def _resolver(path: str, calls: list):
    def resolve(spec, **kwargs):
        calls.append((spec, kwargs))
        return SimpleNamespace(weight_matrix_path=path, lookup_csv_path="unused")

    return resolve


@pytest.mark.parametrize(
    "strategy",
    [None, RowFilterStrategy(variable_name="country", variable_value="ENGLAND")],
)
def test_guard_ignores_regions_without_weight_matrices(monkeypatch, strategy) -> None:
    monkeypatch.setattr(
        "policyengine.data.uk_geography_assets.resolve_uk_geography_asset_paths",
        lambda *args, **kwargs: pytest.fail("no weight matrix should be resolved"),
    )

    sr._require_uk_weight_matrix_matches_dataset(strategy, _dataset(3))


def test_guard_accepts_the_dataset_the_matrix_was_built_for(
    monkeypatch, tmp_path
) -> None:
    calls: list = []
    monkeypatch.setattr(
        "policyengine.data.uk_geography_assets.resolve_uk_geography_asset_paths",
        _resolver(_matrix(tmp_path, households=3), calls),
    )

    sr._require_uk_weight_matrix_matches_dataset(_STRATEGY, _dataset(3))

    [(spec, kwargs)] = calls
    assert spec.weight_matrix_filename == _STRATEGY.weight_matrix_key
    assert spec.lookup_csv_filename == _STRATEGY.lookup_csv_key
    assert spec.resolved_weight_matrix_bucket == _STRATEGY.weight_matrix_bucket
    assert kwargs == {"download_missing_assets": True}


def test_guard_rejects_a_dataset_of_another_size(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "policyengine.data.uk_geography_assets.resolve_uk_geography_asset_paths",
        _resolver(_matrix(tmp_path, households=3), []),
    )

    with pytest.raises(
        ValueError,
        match=(
            r"'E14001063' reweights households with "
            r"parliamentary_constituency_weights\.h5, which was built for 3 "
            r"households; the selected UK dataset has 4"
        ),
    ):
        sr._require_uk_weight_matrix_matches_dataset(_STRATEGY, _dataset(4))


def test_guard_rejects_a_year_the_matrix_does_not_cover(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "policyengine.data.uk_geography_assets.resolve_uk_geography_asset_paths",
        _resolver(_matrix(tmp_path, households=3), []),
    )

    with pytest.raises(ValueError, match=r"has no weights for 2026 \(it covers 2025\)"):
        sr._require_uk_weight_matrix_matches_dataset(_STRATEGY, _dataset(3, 2026))
