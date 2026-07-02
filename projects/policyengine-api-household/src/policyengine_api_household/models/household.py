"""Pydantic models for v1-style household payloads.

Recovered (with light adaptation) from PR #49 (commit 019b808):
libs/policyengine-api/src/policyengine_api/api/models/household.py

The household follows the v1 household API entity structure: each entity
group maps entity names to a mapping of variable name -> {period: value},
where a ``null`` value means "compute this for me". Group entities also
carry a ``members`` list of person keys.
"""

from pydantic import BaseModel, RootModel, Field
from typing import Union, Any, Optional

example_people_us = {
    "you": {"age": {"2024": 40}, "employment_income": {"2024": 29000}},
    "your first dependent": {
        "age": {"2024": 5},
        "employment_income": {"2024": 0},
        "is_tax_unit_dependent": {"2024": True},
    },
}

example_families_us = {"your family": {"members": ["you", "your first dependent"]}}

example_spm_units_us = {"your household": {"members": ["you", "your first dependent"]}}

example_tax_units_us = {
    "your tax unit": {
        "members": ["you", "your first dependent"],
        "eitc": {"2024": 39_000},
        "ctc": {"2024": None},
    }
}

example_households_us = {
    "your household": {
        "members": ["you", "your first dependent"],
        "state_name": {"2024": "CA"},
    }
}

example_marital_units_us = {
    "your marital unit": {"members": ["you"]},
    "your first dependent's marital unit": {
        "members": ["your first dependent"],
        "marital_unit_id": {"2024": 1},
    },
}


example_household_input_us = {
    "people": example_people_us,
    "families": example_families_us,
    "spm_units": example_spm_units_us,
    "tax_units": example_tax_units_us,
    "households": example_households_us,
    "marital_units": example_marital_units_us,
}


class HouseholdAxes(BaseModel):
    name: str  # Variable over which to apply axes
    period: int | str  # The month or year to which the axes apply
    count: int  # The number of axes
    min: int  # The lowest axis
    max: int  # The highest axis


class HouseholdVariable(RootModel):
    root: Union[dict[str, Any], list[str]]


class HouseholdEntity(RootModel):
    root: dict[str, HouseholdVariable]


class HouseholdGeneric(BaseModel):
    households: dict[str, HouseholdEntity] = Field(examples=[example_households_us])
    people: dict[str, HouseholdEntity] = Field(examples=[example_people_us])
    axes: Optional[dict[str, HouseholdAxes]] = None


class HouseholdUS(HouseholdGeneric):
    families: Optional[dict[str, HouseholdEntity]] = Field(
        default={}, examples=[example_families_us]
    )
    spm_units: Optional[dict[str, HouseholdEntity]] = Field(
        default={}, examples=[example_spm_units_us]
    )
    tax_units: Optional[dict[str, HouseholdEntity]] = Field(
        default={}, examples=[example_tax_units_us]
    )
    marital_units: Optional[dict[str, HouseholdEntity]] = Field(
        default={}, examples=[example_marital_units_us]
    )


class HouseholdUK(HouseholdGeneric):
    benunits: Optional[dict[str, HouseholdEntity]] = {}


# Typing alias for all three possible household models
HouseholdData = Union[HouseholdUS, HouseholdUK, HouseholdGeneric]
