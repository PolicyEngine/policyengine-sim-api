"""PR-only preparation and calculation using the installed certified ACS source.

This runs inside an ephemeral Modal function, not a deployed service. It uses
the same region resolver and .py preparation API as request workers, with no
mock loaders, unmanaged inputs, or precomputed year/baseline outputs.
"""

from importlib.metadata import version
from tempfile import TemporaryDirectory
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from policyengine_simulation_executor.simulation_runtime import DatasetSelection


class USRegionalDatasetCheck(BaseModel):
    """Identity and row counts for a successfully prepared Utah calculation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    country: Literal["us"] = "us"
    region: Literal["state/ut"] = "state/ut"
    year: int
    policyengine_version: str
    dataset: str
    artifact_revision: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prepared_person_count: int = Field(gt=0)
    household_count: int = Field(gt=0)
    person_count: int = Field(gt=0)


def _require_certified_regional_selection(
    selection: DatasetSelection,
) -> tuple[str, str]:
    if selection.is_default or not selection.sha256 or not selection.artifact_revision:
        raise ValueError("Utah must select a non-default certified dataset with pins")
    return selection.sha256, selection.artifact_revision


def summarize_us_regional_calculation(
    selection: DatasetSelection,
    prepared_person: pd.DataFrame,
    output_household: pd.DataFrame,
    output_person: pd.DataFrame,
    *,
    year: int,
    policyengine_version: str,
) -> USRegionalDatasetCheck:
    """Reject missing participation, wrong geography, or invalid calculation outputs."""
    sha256, revision = _require_certified_regional_selection(selection)
    for person in (prepared_person, output_person):
        column = "takes_up_wic_if_eligible"
        if (
            person.empty
            or column not in person
            or bool(person[column].isna().any())
            or not pd.api.types.is_bool_dtype(person[column].dtype)
        ):
            raise ValueError("WIC participation must be present and complete booleans")
    state_fips = output_household["state_fips"]
    if (
        output_household.empty
        or bool(state_fips.isna().any())
        or not bool(state_fips.eq(49).all())
    ):
        raise ValueError("Calculated households must be nonempty and scoped to Utah")
    for frame, column in (
        (output_household, "household_net_income"),
        (output_person, "wic"),
    ):
        values = frame[column]
        if (
            not pd.api.types.is_numeric_dtype(values.dtype)
            or not np.isfinite(values.to_numpy()).all()
        ):
            raise ValueError(f"Calculated {column} must contain only finite numbers")
    return USRegionalDatasetCheck(
        year=year,
        policyengine_version=policyengine_version,
        dataset=selection.name,
        artifact_revision=revision,
        source_sha256=sha256,
        prepared_person_count=len(prepared_person),
        household_count=len(output_household),
        person_count=len(output_person),
    )


def check_certified_us_regional_dataset() -> USRegionalDatasetCheck:
    """Prepare real ACS inputs and calculate Utah with the installed .py release."""
    from policyengine.tax_benefit_models.us.datasets import (
        PolicyEngineUSDataset,
        USYearData,
    )

    from policyengine_simulation_executor.simulation_runtime import (
        DEFAULT_YEAR,
        _build_simulation,
        _country_module,
        _resolve_dataset_selection,
        _resolve_region,
    )

    params = {"country": "us", "region": "state/ut", "time_period": str(DEFAULT_YEAR)}
    country = _country_module("us")
    region = _resolve_region(country_module=country, country="us", params=params)
    selection = _resolve_dataset_selection(params, region_resolution=region)
    _require_certified_regional_selection(selection)

    # The production loader calls this exact API. Use an isolated directory
    # here so a previously prepared year cannot hide a broken source/loader.
    # The .py materializer downloads and verifies the bundle-pinned ACS file.
    with TemporaryDirectory(prefix="policyengine-us-regional-check-") as data_folder:
        datasets = country.ensure_datasets(
            datasets=[selection.name], years=[DEFAULT_YEAR], data_folder=data_folder
        )
        dataset = datasets[f"{selection.name}_{DEFAULT_YEAR}"]
        if not isinstance(dataset, PolicyEngineUSDataset) or dataset.data is None:
            raise RuntimeError("ACS preparation produced no US input data")
        prepared_person = dataset.data.person
        simulation = _build_simulation(
            params,
            dataset=dataset,
            dataset_selection=selection,
            policy=None,
            scoping_strategy=region.scoping_strategy,
            region_code=region.code,
        )
        simulation.extra_variables = {
            "household": ["state_fips", "household_net_income"],
            "person": ["takes_up_wic_if_eligible", "wic"],
        }
        simulation.ensure()
        if simulation.output_dataset is None:
            raise RuntimeError("Utah calculation produced no output dataset")
        output_data = simulation.output_dataset.data
        if not isinstance(output_data, USYearData):
            raise TypeError("Utah calculation produced no US entity tables")
        return summarize_us_regional_calculation(
            selection,
            prepared_person,
            output_data.household,
            output_data.person,
            year=DEFAULT_YEAR,
            policyengine_version=version("policyengine"),
        )
