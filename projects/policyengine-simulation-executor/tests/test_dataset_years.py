"""A run for a year the country model cannot project its dataset to fails early.

policyengine-uk and policyengine-us project a single-year dataset forward from
the year its data describes to the last year of the model's economic
assumptions. A UK run outside those years fails inside .py while the year file
is prepared, and a US run outside them silently uses default or carried-over
inputs. Both must instead fail with the stable ``DATASET_YEAR_UNAVAILABLE``
code, naming the requested year and the supported range, before .py prepares
the dataset.

The source datasets here are small synthetic files saved by each model's own
dataset class, and the expected years come from each model's own projection,
so the tests follow the installed model version.
"""

from types import SimpleNamespace

import pytest
from dataset_year_sources import (
    model_projected_years,
    write_synthetic_source,
)
from policyengine_simulation_executor.simulation_runtime import (
    _load_dataset,
    _resolve_dataset_selection,
)

DATASET_YEAR_UNAVAILABLE = "DATASET_YEAR_UNAVAILABLE"
SYNTHETIC_DATA_YEAR = 2024


class RecordingCountryModule:
    """A .py country module that records ``ensure_datasets`` calls.

    Like policyengine-uk, the UK module fails with ``No dataset found`` for a
    year outside the projection; the US module returns a dataset for any year.
    """

    def __init__(self, country, projected_years):
        self.country = country
        self.projected_years = projected_years
        self.ensure_calls = []
        self.dataset = SimpleNamespace(name="prepared year file")

    def ensure_datasets(self, **load_options):
        self.ensure_calls.append(load_options)
        (year,) = load_options["years"]
        if self.country == "uk" and year not in self.projected_years:
            msg = f"No dataset found for year {year}."
            raise ValueError(msg)
        return {"dataset": self.dataset}


@pytest.fixture(params=["uk", "us"])
def country_source(request, tmp_path, monkeypatch):
    """Return a country, its synthetic source file and its projected years."""
    country = request.param
    source_path = write_synthetic_source(
        country, tmp_path / "sources", data_year=SYNTHETIC_DATA_YEAR
    )
    materialized = []

    def materialize_dataset(country_id, dataset=None, **options):
        materialized.append((country_id, dataset, options))
        return SimpleNamespace(path=str(source_path))

    monkeypatch.setattr(
        "policyengine.provenance.dataset_materialization.materialize_dataset",
        materialize_dataset,
    )
    return SimpleNamespace(
        country=country,
        projected_years=model_projected_years(country, source_path),
        materialized=materialized,
        data_folder=tmp_path / "data",
    )


def load_year(country_source, year):
    """Load the country's default dataset for ``year`` with a recording module."""
    country_module = RecordingCountryModule(
        country_source.country, country_source.projected_years
    )
    params = {"country": country_source.country, "time_period": str(year)}
    selection = _resolve_dataset_selection(params)
    country_source.data_folder.mkdir(exist_ok=True)
    dataset = _load_dataset(
        params,
        selection=selection,
        country_module=country_module,
        data_folder=str(country_source.data_folder),
    )
    return dataset, country_module


def assert_dataset_year_unavailable(error, *, country, requested_year, years):
    """Check the stable code, the requested year and the supported range."""
    assert getattr(error, "code", None) == DATASET_YEAR_UNAVAILABLE, (
        f"expected the {DATASET_YEAR_UNAVAILABLE} error, got {error!r}"
    )
    assert error.requested_year == requested_year
    assert error.first_supported_year == min(years)
    assert error.last_supported_year == max(years)
    assert str(error).startswith(
        f"{country.upper()} results are available for {min(years)} to {max(years)}."
    )
    assert str(requested_year) in str(error)


def test_the_synthetic_source_projects_from_its_data_year(country_source):
    # The model projects the source from its data year; the tests below
    # request the years on either side of that projection.
    assert min(country_source.projected_years) == SYNTHETIC_DATA_YEAR
    assert max(country_source.projected_years) > SYNTHETIC_DATA_YEAR


def test_a_year_after_the_projection_fails_before_py_prepares_it(country_source):
    requested_year = max(country_source.projected_years) + 1

    with pytest.raises(ValueError, match="results are available for") as raised:
        load_year(country_source, requested_year)

    assert_dataset_year_unavailable(
        raised.value,
        country=country_source.country,
        requested_year=requested_year,
        years=country_source.projected_years,
    )


def test_a_year_before_the_data_year_fails_before_py_prepares_it(country_source):
    requested_year = SYNTHETIC_DATA_YEAR - 1

    with pytest.raises(ValueError, match="results are available for") as raised:
        load_year(country_source, requested_year)

    assert_dataset_year_unavailable(
        raised.value,
        country=country_source.country,
        requested_year=requested_year,
        years=country_source.projected_years,
    )


def test_an_unsupported_year_never_reaches_ensure_datasets(country_source):
    requested_year = max(country_source.projected_years) + 1
    country_module = RecordingCountryModule(
        country_source.country, country_source.projected_years
    )
    params = {"country": country_source.country, "time_period": str(requested_year)}
    country_source.data_folder.mkdir(exist_ok=True)

    with pytest.raises(ValueError, match="results are available for"):
        _load_dataset(
            params,
            selection=_resolve_dataset_selection(params),
            country_module=country_module,
            data_folder=str(country_source.data_folder),
        )

    assert country_module.ensure_calls == []
