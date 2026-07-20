"""Unit tests for the map-reduce microdata payload (B4)."""

from types import SimpleNamespace

import pandas as pd

from policyengine_simulation_executor.simulation_microdata import (
    country_entities,
    extract_output_microdata,
)


def _fake_sim(net_income):
    data = SimpleNamespace(
        person=pd.DataFrame(
            {"person_id": [1, 2], "household_id": [1, 1], "age": [30, 40]}
        ),
        household=pd.DataFrame(
            {"household_id": [1], "household_net_income": [net_income]}
        ),
    )
    return SimpleNamespace(output_dataset=SimpleNamespace(data=data))


class TestCountryEntities:
    def test__us_has_person_and_group_entities(self):
        entities = country_entities("us")
        assert entities[0] == "person"
        assert set(entities) >= {
            "household",
            "tax_unit",
            "spm_unit",
            "family",
            "marital_unit",
        }

    def test__uk_entities(self):
        assert country_entities("uk") == ["person", "benunit", "household"]


class TestExtractOutputMicrodata:
    def test__dumps_baseline_and_reform_per_entity(self):
        out = extract_output_microdata(
            _fake_sim(100.0), _fake_sim(120.0), ["person", "household"]
        )
        assert out["entities"] == ["person", "household"]
        assert out["baseline"]["household"]["household_net_income"] == [100.0]
        assert out["reform"]["household"]["household_net_income"] == [120.0]

    def test__preserves_id_link_columns(self):
        out = extract_output_microdata(
            _fake_sim(100.0), _fake_sim(100.0), ["person", "household"]
        )
        # The person->household link must survive so map_to_entity stays valid.
        assert out["baseline"]["person"]["household_id"] == [1, 1]
        assert out["baseline"]["person"]["person_id"] == [1, 2]

    def test__roundtrips_back_to_dataframe(self):
        out = extract_output_microdata(_fake_sim(42.0), _fake_sim(42.0), ["household"])
        df = pd.DataFrame(out["baseline"]["household"])
        assert list(df["household_net_income"]) == [42.0]
