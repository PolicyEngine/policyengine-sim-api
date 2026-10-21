"""Small US year files for the per-state dataset tests.

The files are written with policyengine.py's own ``PolicyEngineUSDataset.save()``
and year-file stamp, and carry the SPM record the installed release expects
for the regional dataset, so .py's reuse checks treat them as it would a year
file ``create_datasets`` wrote. Three households: two in Utah (state FIPS 49)
and one in Wyoming (56), with uneven weights on every entity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from fixtures.weighted_frames import weighted_entity_frame

if TYPE_CHECKING:
    from pathlib import Path

    from microdf import MicroDataFrame

REGIONAL_DATASET = "populace_us_2024_acs_local"
UTAH_FIPS = 49
WYOMING_FIPS = 56


def source_entity_frames(
    *, wyoming_district_geoid: int = 5600
) -> dict[str, MicroDataFrame]:
    """Return the six US entity frames of the three-household sample."""
    return {
        "household": weighted_entity_frame(
            {
                "household_id": [1, 2, 3],
                "household_weight": [10.0, 20.0, 30.0],
                "state_fips": [UTAH_FIPS, UTAH_FIPS, WYOMING_FIPS],
                "congressional_district_geoid": [4901, 4902, wyoming_district_geoid],
            }
        ),
        "person": weighted_entity_frame(
            {
                "person_id": [1, 2, 3, 4],
                "person_household_id": [1, 1, 2, 3],
                "person_tax_unit_id": [1, 1, 2, 3],
                "person_spm_unit_id": [1, 1, 2, 3],
                "person_family_id": [1, 1, 2, 3],
                "person_marital_unit_id": [1, 2, 3, 4],
                "person_weight": [10.0, 11.0, 20.0, 30.0],
                "age": [40, 12, 70, 33],
            },
            entity="person",
        ),
        "tax_unit": weighted_entity_frame(
            {"tax_unit_id": [1, 2, 3], "tax_unit_weight": [10.0, 20.0, 30.0]},
            entity="tax_unit",
        ),
        "spm_unit": weighted_entity_frame(
            {
                "spm_unit_id": [1, 2, 3],
                "spm_unit_weight": [10.0, 20.0, 30.0],
                "spm_unit_spm_universe_status": ["INCLUDED"] * 3,
            },
            entity="spm_unit",
        ),
        "family": weighted_entity_frame(
            {"family_id": [1, 2, 3], "family_weight": [10.0, 20.0, 30.0]},
            entity="family",
        ),
        "marital_unit": weighted_entity_frame(
            {
                "marital_unit_id": [1, 2, 3, 4],
                "marital_unit_weight": [10.0, 11.0, 20.0, 30.0],
            },
            entity="marital_unit",
        ),
    }


def year_file_metadata(dataset: str = REGIONAL_DATASET) -> dict:
    """Return the metadata a ``create_datasets`` year file of ``dataset`` carries."""
    from policyengine.tax_benefit_models.us.datasets import (
        _expected_legacy_spm_profile,
    )
    from policyengine.tax_benefit_models.us.legacy_inputs import RENAMES_RECORD_KEY
    from policyengine.tax_benefit_models.us.legacy_spm.record import (
        RECORD_KEY,
        LegacySPMRecord,
    )

    metadata: dict = {RENAMES_RECORD_KEY: {}}
    profile = _expected_legacy_spm_profile(dataset)
    if profile is not None:
        metadata[RECORD_KEY] = LegacySPMRecord(
            profile=profile, outside_spm_units=0, included_spm_units=3
        ).model_dump(mode="json")
    return metadata


def write_year_file(
    folder: Path,
    *,
    year: int,
    dataset: str = REGIONAL_DATASET,
    entity_frames: dict[str, MicroDataFrame] | None = None,
    stamp: bool = True,
) -> Path:
    """Write ``<folder>/<dataset>_year_<year>.h5`` as ``create_datasets`` would."""
    from policyengine.tax_benefit_models.us.datasets import (
        PolicyEngineUSDataset,
        USYearData,
    )
    from policyengine.tax_benefit_models.us.legacy_inputs import (
        write_year_file_stamp,
        year_file_stamp,
    )

    path = folder / f"{dataset}_year_{year}.h5"
    PolicyEngineUSDataset(
        name=f"{dataset}-year-{year}",
        description=f"Test year file for {year}",
        filepath=str(path),
        year=year,
        metadata=year_file_metadata(dataset),
        data=USYearData(**(entity_frames or source_entity_frames())),
    ).save()
    if stamp:
        write_year_file_stamp(path, year_file_stamp())
    return path


def stored_ids(path: Path, entity: str) -> list[int]:
    """Return an entity's ids as stored in a year file, in row order."""
    with pd.HDFStore(path, mode="r") as store:
        return [int(value) for value in store[entity][f"{entity}_id"]]


def stored_weights(path: Path, entity: str) -> list[float]:
    """Return an entity's stored weights, in row order."""
    with pd.HDFStore(path, mode="r") as store:
        return [float(value) for value in store[entity][f"{entity}_weight"]]
