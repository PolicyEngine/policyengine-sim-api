"""The checkable shapes of a simulation's output data."""

from types import SimpleNamespace

import pandas as pd
from microdf import MicroDataFrame
from policyengine.tax_benefit_models.uk.datasets import UKYearData
from policyengine_simulation_executor.output_frames import EntityFrames


def _uk_year_data() -> UKYearData:
    def frame(entity: str) -> MicroDataFrame:
        return MicroDataFrame(
            pd.DataFrame({f"{entity}_id": [1], f"{entity}_weight": [2.0]}),
            weights=f"{entity}_weight",
        )

    return UKYearData(
        person=frame("person"), benunit=frame("benunit"), household=frame("household")
    )


def test_country_year_data_has_entity_frames():
    year_data = _uk_year_data()

    assert isinstance(year_data, EntityFrames)


def test_data_with_frames_by_attribute_matches_the_shape():
    assert isinstance(SimpleNamespace(entity_data={}), EntityFrames)


def test_missing_output_data_does_not_match_the_shape():
    assert not isinstance(None, EntityFrames)
    assert not isinstance(SimpleNamespace(household=None), EntityFrames)
