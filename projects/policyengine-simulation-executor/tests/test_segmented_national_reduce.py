"""Unit tests for the segmented-national reduce (C4).

Full-fidelity reduce-vs-direct parity runs on staging (C7); these tests pin
the pure mechanics: concatenation order, dtype restoration, the no-recompute
guarantee of PrecomputedSimulation, and the builder wiring.
"""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from policyengine_simulation_executor import segmented_national_reduce as reduce_mod
from policyengine_simulation_executor.segmented_national_reduce import (
    PrecomputedSimulation,
    _us_dataset_from_frames,
    build_national_output,
    concatenate_microdata,
)


def _child(household_ids, incomes, **extra):
    """A minimal child output dict with a _microdata payload."""
    frame = {
        "household_id": list(household_ids),
        "household_net_income": [float(x) for x in incomes],
        "household_weight": [1.0] * len(household_ids),
    }
    dtypes = {
        "household_id": "int64",
        "household_net_income": "float32",
        "household_weight": "float32",
    }
    return {
        "budget": {"budgetary_impact": -1.0},
        **extra,
        "_microdata": {
            "baseline": {"household": frame},
            "reform": {"household": frame},
            "dtypes": {
                "baseline": {"household": dtypes},
                "reform": {"household": dtypes},
            },
        },
    }


class TestConcatenateMicrodata:
    def test__concatenates_in_child_order(self):
        sides = concatenate_microdata([_child([1, 2], [10, 20]), _child([3], [30])])
        household = sides["baseline"]["household"]
        assert list(household["household_id"]) == [1, 2, 3]
        assert list(household["household_net_income"]) == [10.0, 20.0, 30.0]

    def test__restores_source_dtypes(self):
        sides = concatenate_microdata([_child([1], [10]), _child([2], [20])])
        assert str(sides["reform"]["household"]["household_net_income"].dtype) == (
            "float32"
        )

    def test__rejects_child_without_microdata(self):
        with pytest.raises(ValueError, match="no _microdata"):
            concatenate_microdata([_child([1], [10]), {"budget": {}}])

    def test__rejects_empty_input(self):
        with pytest.raises(ValueError, match="No child outputs"):
            concatenate_microdata([])


class TestPrecomputedSimulation:
    def test__ensure_never_computes(self, monkeypatch):
        def boom(self):
            raise AssertionError("RECOMPUTE ATTEMPTED")

        monkeypatch.setattr(PrecomputedSimulation, "run", boom)
        simulation = PrecomputedSimulation()
        assert simulation.ensure() is None


class TestUSDatasetFromFrames:
    def test__wraps_entities_with_weight_columns(self):
        frames = {
            entity: pd.DataFrame(
                {
                    f"{entity}_id": [1, 2],
                    f"{entity}_weight": np.array([2.0, 3.0], dtype=np.float32),
                }
            )
            for entity in (
                "person",
                "marital_unit",
                "family",
                "spm_unit",
                "tax_unit",
                "household",
            )
        }
        dataset = _us_dataset_from_frames(frames, year=2026)
        assert dataset.year == 2026
        assert dataset.filepath is None
        assert float(dataset.data.household.weights.sum()) == 5.0


class TestBuildNationalOutputWiring:
    def test__runs_existing_builder_over_stand_ins(self, monkeypatch):
        captured = {}

        class FakeBuilder:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def serialize(self):
                return {"budget": {"budgetary_impact": -42.0}}

        class FakeStandIn:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.output_dataset = None

        monkeypatch.setattr(reduce_mod, "SimulationOutputBuilder", FakeBuilder)
        monkeypatch.setattr(reduce_mod, "PrecomputedSimulation", FakeStandIn)
        monkeypatch.setattr(
            reduce_mod,
            "_us_dataset_from_frames",
            lambda frames, *, year: SimpleNamespace(frames=frames, year=year),
        )

        country_module = SimpleNamespace(model=object())
        result = build_national_output(
            [_child([1], [10]), _child([2], [20])],
            country="us",
            simulation_params={"country": "us", "scope": "macro"},
            country_module=country_module,
            year=2026,
        )

        assert result == {"budget": {"budgetary_impact": -42.0}}
        assert captured["resolved_region_code"] == "us"
        assert captured["country"] == "us"
        # Both stand-ins carry the injected output_dataset.
        assert captured["baseline"].output_dataset is captured["baseline"].kwargs[
            "dataset"
        ]
        assert captured["reform"].output_dataset is captured["reform"].kwargs[
            "dataset"
        ]
        assert captured["baseline"].kwargs["tax_benefit_model_version"] is (
            country_module.model
        )

    def test__rejects_non_us(self):
        with pytest.raises(ValueError, match="US only"):
            build_national_output(
                [_child([1], [10])],
                country="uk",
                simulation_params={},
                country_module=SimpleNamespace(model=object()),
                year=2026,
            )

    def test__propagates_child_versions_over_builder_fallbacks(
        self, monkeypatch
    ):
        # The rebuilt dataset has no artifact metadata, so the builder's
        # version fallbacks misreport provenance; the children loaded the
        # real artifact and their reported versions win.
        class FakeBuilder:
            def __init__(self, **kwargs):
                pass

            def serialize(self):
                return {
                    "model_version": "bundle-fallback",
                    "data_version": "bundle-fallback",
                }

        monkeypatch.setattr(reduce_mod, "SimulationOutputBuilder", FakeBuilder)
        monkeypatch.setattr(
            reduce_mod,
            "PrecomputedSimulation",
            lambda **kwargs: SimpleNamespace(output_dataset=None, **kwargs),
        )
        monkeypatch.setattr(
            reduce_mod,
            "_us_dataset_from_frames",
            lambda frames, *, year: SimpleNamespace(frames=frames, year=year),
        )

        children = [
            _child([1], [10], model_version="1.764.6", data_version="buildm-x"),
            _child([2], [20], model_version="1.764.6", data_version="buildm-x"),
        ]
        result = build_national_output(
            children,
            country="us",
            simulation_params={"country": "us", "scope": "macro"},
            country_module=SimpleNamespace(model=object()),
            year=2026,
        )
        assert result["model_version"] == "1.764.6"
        assert result["data_version"] == "buildm-x"
