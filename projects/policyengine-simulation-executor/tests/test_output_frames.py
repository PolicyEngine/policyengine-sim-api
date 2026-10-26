"""The checkable shapes of a simulation's output data."""

from types import SimpleNamespace

import pandas as pd
import pytest
from microdf import MicroDataFrame
from policyengine.tax_benefit_models.common.model_version import (
    MicrosimulationModelVersion,
)
from policyengine.tax_benefit_models.uk.datasets import UKYearData
from policyengine.tax_benefit_models.us.datasets import USYearData
from policyengine_simulation_executor.baseline_artifacts import EntityVariableResolver
from policyengine_simulation_executor.output_frames import EntityFrames
from policyengine_simulation_executor.simulation_output_common import _entity_data


def _frame(entity: str) -> MicroDataFrame:
    return MicroDataFrame(
        pd.DataFrame({f"{entity}_id": [1], f"{entity}_weight": [2.0]}),
        weights=f"{entity}_weight",
    )


def _uk_year_data() -> UKYearData:
    return UKYearData(
        person=_frame("person"),
        benunit=_frame("benunit"),
        household=_frame("household"),
    )


def _us_year_data() -> USYearData:
    return USYearData(
        **{
            entity: _frame(entity)
            for entity in (
                "person",
                "marital_unit",
                "family",
                "spm_unit",
                "tax_unit",
                "household",
            )
        }
    )


@pytest.mark.parametrize(
    "year_data_factory", [_uk_year_data, _us_year_data], ids=["UK", "US"]
)
def test_country_year_data_has_entity_frames(year_data_factory):
    year_data = year_data_factory()

    assert isinstance(year_data, EntityFrames)


def test_country_model_versions_resolve_entity_variables():
    # policyengine's US and UK model versions both derive from this class.
    assert issubclass(MicrosimulationModelVersion, EntityVariableResolver)


def test_data_with_frames_by_attribute_matches_the_shape():
    assert isinstance(SimpleNamespace(entity_data={}), EntityFrames)


def test_missing_output_data_does_not_match_the_shape():
    assert not isinstance(None, EntityFrames)
    assert not isinstance(SimpleNamespace(household=None), EntityFrames)


class _SimulationWithoutOutput:
    """A simulation whose ensure() leaves the output it was given."""

    def __init__(self, output_dataset):
        self.output_dataset = output_dataset

    def ensure(self):
        pass


@pytest.mark.parametrize(
    "output_dataset",
    [None, SimpleNamespace(data=None)],
    ids=["no output dataset", "no output data"],
)
def test_reading_an_entity_frame_without_output_data_raises_type_error(
    output_dataset,
):
    with pytest.raises(TypeError, match="no output data"):
        _entity_data(_SimulationWithoutOutput(output_dataset), "household")
