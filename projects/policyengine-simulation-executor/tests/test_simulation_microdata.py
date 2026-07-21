"""Unit tests for the map-reduce microdata payload (B4 + C3 dtype transport)."""

from types import SimpleNamespace

import numpy as np
import pandas as pd

from policyengine_simulation_executor.simulation_microdata import (
    extract_output_microdata,
    rebuild_entity_frame,
)


def _fake_sim(net_income):
    # Mirrors YearData: entity_data maps entity name -> its frame.
    data = SimpleNamespace(
        entity_data={
            "person": pd.DataFrame(
                {"person_id": [1, 2], "household_id": [1, 1], "age": [30, 40]}
            ),
            "household": pd.DataFrame(
                {
                    "household_id": [1],
                    "household_net_income": np.array([net_income], dtype=np.float32),
                }
            ),
        }
    )
    return SimpleNamespace(output_dataset=SimpleNamespace(data=data))


class TestExtractOutputMicrodata:
    def test__entity_tables_derive_from_the_models_entity_data(self):
        out = extract_output_microdata(_fake_sim(100.0), _fake_sim(120.0))
        # The payload's entity list IS the baseline dict's keys — no
        # separate "entities" field to drift out of sync.
        assert list(out["baseline"]) == ["person", "household"]
        assert list(out["reform"]) == ["person", "household"]

    def test__dumps_baseline_and_reform_per_entity(self):
        out = extract_output_microdata(_fake_sim(100.0), _fake_sim(120.0))
        assert out["baseline"]["household"]["household_net_income"] == [100.0]
        assert out["reform"]["household"]["household_net_income"] == [120.0]

    def test__preserves_id_link_columns(self):
        out = extract_output_microdata(_fake_sim(100.0), _fake_sim(100.0))
        # The person->household link must survive so map_to_entity stays valid.
        assert out["baseline"]["person"]["household_id"] == [1, 1]
        assert out["baseline"]["person"]["person_id"] == [1, 2]

    def test__carries_source_dtypes_per_side_and_entity(self):
        out = extract_output_microdata(_fake_sim(100.0), _fake_sim(120.0))
        for side in ("baseline", "reform"):
            assert (
                out["dtypes"][side]["household"]["household_net_income"] == "float32"
            )
            assert "person_id" in out["dtypes"][side]["person"]


class TestRebuildEntityFrame:
    def test__float32_column_survives_the_round_trip(self):
        # The B5 regression: a plain to_dict("list") -> DataFrame round trip
        # widens float32 to float64 and shifts weighted aggregates ~1e-7.
        out = extract_output_microdata(_fake_sim(42.0), _fake_sim(42.0))
        df = rebuild_entity_frame(
            out["baseline"]["household"], out["dtypes"]["baseline"]["household"]
        )
        assert str(df["household_net_income"].dtype) == "float32"
        assert list(df["household_net_income"]) == [np.float32(42.0)]

    def test__missing_dtypes_fall_back_to_inference(self):
        df = rebuild_entity_frame({"x": [1, 2]}, None)
        assert list(df["x"]) == [1, 2]

    def test__uncastable_dtype_is_tolerated(self):
        df = rebuild_entity_frame({"x": ["a", "b"]}, {"x": "float32"})
        assert list(df["x"]) == ["a", "b"]
