"""The years a country model projects a simulation dataset to.

policyengine-uk and policyengine-us each project a single-year source dataset
forward with ``extend_single_year_dataset``: from the year its data describes
(its data year) to the last year of the model's economic assumptions. Outside
those years there is no supported result. .py fails while it prepares a UK
year file ("No dataset found for year ..."), and a US year file holds default
inputs before the data year and carried-over inputs after the last year.

``require_supported_dataset_year`` refuses such a year before .py prepares the
dataset, with ``DatasetYearUnavailableError``, which the gateway returns as a
400 response. The supported years come from the source file and the installed
model, so they move with the model version.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
from policyengine_simulation_contract.input_errors import DatasetYearUnavailableError

from policyengine_simulation_executor.artifact_keys import dataset_artifact_filename

if TYPE_CHECKING:
    from policyengine_core.parameters import ParameterNode

logger = logging.getLogger(__name__)

# The HDF5 key each model's own single-year dataset class reads a source
# file's data year from: policyengine-uk's UKSingleYearDataset and
# policyengine-us's USSingleYearDataset.
_DATA_YEAR_KEYS = {"uk": "time_period", "us": "_time_period"}
_YEAR_DIGITS = 4


@dataclass(frozen=True)
class SupportedYears:
    """The first and last year, inclusive, a model projects a dataset to."""

    first: int
    last: int

    def __contains__(self, year: object) -> bool:
        """Return whether ``year`` is an integer year in the range."""
        return isinstance(year, int) and self.first <= year <= self.last


def model_projection_end_year(country: str) -> int:
    """Return the last year the installed model projects a single-year dataset to.

    This is the end year each model's ``extend_single_year_dataset`` uses by
    default: policyengine-uk's ``PROJECTION_END_YEAR``, shortened to the end
    of a capital gains projection that stops earlier, and the last year
    written in policyengine-us's CPI-U parameter file.
    """
    if country == "uk":
        from policyengine_uk.data.capital_gains_projection import CGT_INDEX_PATH
        from policyengine_uk.data.economic_assumptions import PROJECTION_END_YEAR
        from policyengine_uk.system import system as uk_system

        parameters = _loaded_parameters(uk_system.parameters, country)
        projection = parameters.get_child(CGT_INDEX_PATH).metadata["projection"]
        return min(
            PROJECTION_END_YEAR,
            int(projection.get("end_year", PROJECTION_END_YEAR)),
        )
    if country == "us":
        from policyengine_us.data.economic_assumptions import (
            CPI_U_PARAM_PATH,
            get_parameter_last_year,
        )
        from policyengine_us.system import system as us_system

        parameters = _loaded_parameters(us_system.parameters, country)
        return get_parameter_last_year(parameters.get_child(CPI_U_PARAM_PATH))
    msg = f"Unsupported country: {country}"
    raise ValueError(msg)


def _loaded_parameters(parameters: ParameterNode | None, country: str) -> ParameterNode:
    """Return a tax-benefit system's parameter tree, which a loaded model has."""
    if parameters is None:
        msg = f"The installed {country.upper()} model has no parameters loaded"
        raise RuntimeError(msg)
    return parameters


def dataset_data_year(country: str, dataset_path: str | Path) -> int | None:
    """Return the year a single-year source dataset's data describes, or None.

    Reads the key the country model's own single-year dataset class reads.
    None when the file has no such key, as for a file in policyengine-core's
    variable-by-period layout.
    """
    key = _DATA_YEAR_KEYS[country]
    with pd.HDFStore(dataset_path, mode="r") as store:
        if key not in store:
            return None
        recorded = pd.Series(store[key]).iloc[0]
    return int(str(recorded)[:_YEAR_DIGITS])


def supported_dataset_years(country: str, data_year: int) -> SupportedYears:
    """Return the years the installed model projects a dataset of ``data_year`` to.

    From the data year to the model's projection end year, as each model's
    ``extend_single_year_dataset`` does by default, and never less than the
    data year itself.
    """
    return SupportedYears(
        first=data_year,
        last=max(data_year, model_projection_end_year(country)),
    )


def require_supported_dataset_year(
    country: str,
    year: int,
    *,
    dataset_name: str,
    data_folder: str,
) -> None:
    """Raise ``DatasetYearUnavailableError`` unless the dataset covers ``year``.

    A year whose year file ``ensure_datasets`` would load from ``data_folder``
    was prepared from this dataset before, so it is supported and nothing is
    read. Otherwise the source file is materialized where .py's
    ``create_datasets`` materializes it next, and ``year`` is checked against
    its data year and the installed model's projection end year. A source
    that records no data year is not checked.
    """
    from policyengine.provenance.dataset_materialization import materialize_dataset
    from policyengine.provenance.manifest import (
        dataset_logical_name,
        resolve_dataset_reference,
    )

    stem = dataset_logical_name(resolve_dataset_reference(country, dataset_name))
    if (Path(data_folder) / dataset_artifact_filename(stem, year)).is_file():
        return
    source = materialize_dataset(country, dataset_name, data_dir=Path(data_folder))
    data_year = dataset_data_year(country, source.path)
    if data_year is None:
        logger.warning(
            "Not checking year %s of %s: its source %s records no data year",
            year,
            dataset_name,
            source.path,
        )
        return
    supported = supported_dataset_years(country, data_year)
    if year not in supported:
        raise DatasetYearUnavailableError(
            country, year, supported.first, supported.last
        )
