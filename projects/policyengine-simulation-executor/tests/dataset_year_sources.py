"""Small synthetic source datasets, saved by each country model's own classes.

Three households, written in the single-year HDF5 layout the model reads, so a
test can ask the installed model which years it projects the file to without
downloading a certified dataset.
"""

from pathlib import Path

import numpy as np
import pandas as pd

HOUSEHOLD_IDS = np.arange(1, 4)
HOUSEHOLD_WEIGHTS = [2.0, 3.0, 5.0]
AGES = [45, 30, 70]
EMPLOYMENT_INCOME = [30_000.0, 0.0, 0.0]


def _uk_single_year_dataset(data_year):
    from policyengine_uk.data.dataset_schema import UKSingleYearDataset

    return UKSingleYearDataset(
        person=pd.DataFrame(
            {
                "person_id": HOUSEHOLD_IDS,
                "person_benunit_id": HOUSEHOLD_IDS,
                "person_household_id": HOUSEHOLD_IDS,
                "age": AGES,
                "employment_income": EMPLOYMENT_INCOME,
            }
        ),
        benunit=pd.DataFrame({"benunit_id": HOUSEHOLD_IDS}),
        household=pd.DataFrame(
            {
                "household_id": HOUSEHOLD_IDS,
                "household_weight": HOUSEHOLD_WEIGHTS,
                "region": "LONDON",
                "tenure_type": "OWNED_OUTRIGHT",
                "rent": 0.0,
                "council_tax": 0.0,
            }
        ),
        fiscal_year=data_year,
    )


def _us_single_year_dataset(data_year):
    from policyengine_us.data.dataset_schema import USSingleYearDataset

    person_links = {
        f"person_{entity}_id": HOUSEHOLD_IDS
        for entity in ("household", "tax_unit", "spm_unit", "family", "marital_unit")
    }
    return USSingleYearDataset(
        person=pd.DataFrame(
            {
                "person_id": HOUSEHOLD_IDS,
                **person_links,
                "age": AGES,
                "employment_income": EMPLOYMENT_INCOME,
            }
        ),
        household=pd.DataFrame(
            {"household_id": HOUSEHOLD_IDS, "household_weight": HOUSEHOLD_WEIGHTS}
        ),
        tax_unit=pd.DataFrame({"tax_unit_id": HOUSEHOLD_IDS}),
        spm_unit=pd.DataFrame({"spm_unit_id": HOUSEHOLD_IDS}),
        family=pd.DataFrame({"family_id": HOUSEHOLD_IDS}),
        marital_unit=pd.DataFrame({"marital_unit_id": HOUSEHOLD_IDS}),
        time_period=data_year,
    )


def write_synthetic_source(country, folder, *, data_year):
    """Save a three-household source dataset for ``data_year``; return its path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"synthetic_{country}_{data_year}.h5"
    if country == "uk":
        _uk_single_year_dataset(data_year).save(str(path))
    else:
        _us_single_year_dataset(data_year).save(str(path))
    return path


def model_projected_years(country, source_path):
    """Return the years the installed model's own projection gives the file."""
    if country == "uk":
        from policyengine_uk.data.dataset_schema import UKSingleYearDataset
        from policyengine_uk.data.economic_assumptions import (
            extend_single_year_dataset,
        )
        from policyengine_uk.system import system

        projected = extend_single_year_dataset(
            UKSingleYearDataset(file_path=str(source_path)), system.parameters
        )
    else:
        from policyengine_us.data.dataset_schema import USSingleYearDataset
        from policyengine_us.data.economic_assumptions import (
            extend_single_year_dataset,
        )

        projected = extend_single_year_dataset(
            USSingleYearDataset(file_path=str(source_path))
        )
    return sorted(int(year) for year in projected.years)
