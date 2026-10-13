"""PR-only preparation and calculation using the installed certified ACS source.

This runs inside an ephemeral Modal function, not a deployed service. It uses
the same region resolver and .py preparation API as request workers, with no
mock loaders, unmanaged inputs, or precomputed year/baseline outputs.
"""

from collections.abc import Mapping
from importlib.metadata import version
from tempfile import TemporaryDirectory
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from policyengine_simulation_executor.simulation_runtime import DatasetSelection

#: .py's backward-compatibility scope rule for legacy certified sources such
#: as the ACS-local Build O release: ACS group quarters are outside the SPM
#: measurement (PolicyEngine/policyengine.py#597).
SPM_MEASUREMENT_SCOPE_RULE = "acs_household_population"

#: Output metadata key of .py's legacy SPM declarations record.
LEGACY_SPM_RECORD_KEY = "legacy_spm_declarations"


class USSPMMeasurementCheck(BaseModel):
    """SPM measurement on the certified ACS-local source.

    Group-quarters SPM units are outside the measurement, so their SPM outcomes
    are missing; every other unit must be measured.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_rule: Literal["acs_household_population"]
    model_mode: Literal["native", "override"]
    outside_spm_unit_count: int = Field(gt=0)
    measured_spm_unit_count: int = Field(gt=0)
    spm_poverty_rate: float = Field(gt=0, lt=1)
    deep_spm_poverty_rate: float = Field(ge=0, lt=1)


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
    spm_measurement: USSPMMeasurementCheck


def _require_certified_regional_selection(
    selection: DatasetSelection,
) -> tuple[str, str]:
    if selection.is_default or not selection.sha256 or not selection.artifact_revision:
        raise ValueError("Utah must select a non-default certified dataset with pins")
    return selection.sha256, selection.artifact_revision


def summarize_us_spm_measurement(
    output_spm_unit: pd.DataFrame,
    poverty_rates: Mapping[str, float],
    scope_record: Mapping[str, Any] | None,
    *,
    source_sha256: str,
) -> USSPMMeasurementCheck:
    """Reject SPM outputs that skip, leak or mis-scope the measurement.

    Outside units are those with a missing SPM poverty indicator. Their SPM
    outcomes must all be missing, and every measured unit's must be finite.
    """
    profile = (scope_record or {}).get("profile") or {}
    source = profile.get("source") or {}
    model_mode = (scope_record or {}).get("model_mode")
    if (
        source.get("spm_universe_rule") != SPM_MEASUREMENT_SCOPE_RULE
        or source.get("source_sha256") != source_sha256
        or model_mode not in ("native", "override")
    ):
        raise ValueError(
            "The calculation must record the legacy SPM measurement scope "
            "for the selected source"
        )
    columns = [
        "spm_unit_is_in_spm_poverty",
        "spm_unit_is_in_deep_spm_poverty",
        "spm_unit_net_income",
    ]
    if output_spm_unit.empty or any(
        column not in output_spm_unit
        or not pd.api.types.is_float_dtype(output_spm_unit[column].dtype)
        for column in columns
    ):
        raise ValueError("SPM outcomes must be present as floats")
    outcomes = output_spm_unit[columns].to_numpy(dtype=float)
    outside = np.isnan(outcomes[:, 0])
    if not outside.any():
        raise ValueError(
            "Utah must have group-quarters SPM units outside the measurement"
        )
    if not np.isnan(outcomes[outside]).all():
        raise ValueError(
            "SPM outcomes of units outside the measurement must be missing"
        )
    if outside.all() or not np.isfinite(outcomes[~outside]).all():
        raise ValueError("SPM outcomes of measured units must be finite")
    if not np.isin(outcomes[~outside, :2], (0.0, 1.0)).all():
        raise ValueError("Measured SPM poverty indicators must be 0 or 1")
    rate = poverty_rates.get("spm")
    deep_rate = poverty_rates.get("spm_deep")
    if (
        rate is None
        or deep_rate is None
        or not np.isfinite([rate, deep_rate]).all()
        or deep_rate > rate
    ):
        raise ValueError(
            "SPM poverty rates must be finite, with deep poverty no higher"
        )
    return USSPMMeasurementCheck(
        scope_rule=SPM_MEASUREMENT_SCOPE_RULE,
        model_mode=model_mode,
        outside_spm_unit_count=int(outside.sum()),
        measured_spm_unit_count=int((~outside).sum()),
        spm_poverty_rate=rate,
        deep_spm_poverty_rate=deep_rate,
    )


def summarize_us_regional_calculation(
    selection: DatasetSelection,
    prepared_person: pd.DataFrame,
    output_household: pd.DataFrame,
    output_person: pd.DataFrame,
    *,
    year: int,
    policyengine_version: str,
    spm_measurement: USSPMMeasurementCheck,
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
        spm_measurement=spm_measurement,
    )


def check_certified_us_regional_dataset() -> USRegionalDatasetCheck:
    """Prepare real ACS inputs and calculate Utah with the installed .py release."""
    from policyengine.outputs.poverty import calculate_us_poverty_rates
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
        rates = calculate_us_poverty_rates(simulation).dataframe
        spm_measurement = summarize_us_spm_measurement(
            pd.DataFrame(output_data.spm_unit),
            dict(zip(rates["poverty_type"], rates["rate"], strict=True)),
            simulation.output_dataset.metadata.get(LEGACY_SPM_RECORD_KEY),
            source_sha256=selection.sha256 or "",
        )
        return summarize_us_regional_calculation(
            selection,
            prepared_person,
            output_data.household,
            output_data.person,
            year=DEFAULT_YEAR,
            policyengine_version=version("policyengine"),
            spm_measurement=spm_measurement,
        )
