"""Unit tests for the map-reduce microdata payload (B4)."""

from types import SimpleNamespace

import pandas as pd

from policyengine_simulation_executor.simulation_microdata import (
    extract_output_microdata,
)


def _fake_sim(net_income):
    # Mirrors YearData: entity_data maps entity name -> its frame.
    data = SimpleNamespace(
        entity_data={
            "person": pd.DataFrame(
                {"person_id": [1, 2], "household_id": [1, 1], "age": [30, 40]}
            ),
            "household": pd.DataFrame(
                {"household_id": [1], "household_net_income": [net_income]}
            ),
        }
    )
    return SimpleNamespace(output_dataset=SimpleNamespace(data=data))


class TestExtractOutputMicrodata:
    def test__entities_derive_from_the_models_entity_data(self):
        out = extract_output_microdata(_fake_sim(100.0), _fake_sim(120.0))
        assert out["entities"] == ["person", "household"]

    def test__dumps_baseline_and_reform_per_entity(self):
        out = extract_output_microdata(_fake_sim(100.0), _fake_sim(120.0))
        assert out["baseline"]["household"]["household_net_income"] == [100.0]
        assert out["reform"]["household"]["household_net_income"] == [120.0]

    def test__preserves_id_link_columns(self):
        out = extract_output_microdata(_fake_sim(100.0), _fake_sim(100.0))
        # The person->household link must survive so map_to_entity stays valid.
        assert out["baseline"]["person"]["household_id"] == [1, 1]
        assert out["baseline"]["person"]["person_id"] == [1, 2]

    def test__roundtrips_back_to_dataframe(self):
        out = extract_output_microdata(_fake_sim(42.0), _fake_sim(42.0))
        df = pd.DataFrame(out["baseline"]["household"])
        assert list(df["household_net_income"]) == [42.0]
