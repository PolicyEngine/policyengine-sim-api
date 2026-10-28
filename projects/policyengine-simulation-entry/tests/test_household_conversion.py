"""Stored v2 households, in the situation shape the household functions take.

These cases come from the Public API, which converted households before the
Simulation Entrypoint read them itself.
"""

from __future__ import annotations

from policyengine_simulation_entry.household_conversion import (
    household_situation,
    saved_spm_choice,
)

US_HOUSEHOLD = {
    "people": [
        {
            "id": "person-1",
            "source_name": "you",
            "values": {"age": {"2026": 40}, "employment_income": {"2026": 30000}},
            "memberships": {
                "household": "household-1",
                "family": "family-1",
                "tax_unit": "tax_unit-1",
                "spm_unit": "spm_unit-1",
                "marital_unit": "marital_unit-1",
            },
        },
        {
            "id": "person-2",
            "values": {"age": {"2026": 8}},
            "memberships": {
                "household": "household-1",
                "family": "family-1",
                "tax_unit": "tax_unit-1",
                "spm_unit": "spm_unit-1",
                "marital_unit": "marital_unit-2",
            },
        },
    ],
    "household": [{"id": "household-1", "values": {"state_name": {"2026": "CA"}}}],
    "family": [{"id": "family-1", "values": {}}],
    "tax_unit": [{"id": "tax_unit-1", "values": {"eitc": {"2026": None}}}],
    "spm_unit": [{"id": "spm_unit-1", "values": {}}],
    "marital_unit": [
        {"id": "marital_unit-1", "values": {}},
        {"id": "marital_unit-2", "values": {}},
    ],
    "spm": {"scenario": "ce_trend", "geography_kind": "national"},
}
UK_HOUSEHOLD = {
    "people": [
        {
            "id": "person-1",
            "values": {"age": {"2026": 30}},
            "memberships": {"household": "household-1", "benunit": "benunit-1"},
        }
    ],
    "household": [{"id": "household-1", "values": {"region": {"2026": "WALES"}}}],
    "benunit": [{"id": "benunit-1", "values": {"universal_credit": {"2026": None}}}],
}


def test_a_us_household_becomes_a_situation_keyed_by_record_id() -> None:
    assert household_situation("us", US_HOUSEHOLD) == {
        "people": {
            "person-1": {"age": {"2026": 40}, "employment_income": {"2026": 30000}},
            "person-2": {"age": {"2026": 8}},
        },
        "households": {
            "household-1": {
                "members": ["person-1", "person-2"],
                "state_name": {"2026": "CA"},
            }
        },
        "families": {"family-1": {"members": ["person-1", "person-2"]}},
        "tax_units": {
            "tax_unit-1": {"members": ["person-1", "person-2"], "eitc": {"2026": None}}
        },
        "spm_units": {"spm_unit-1": {"members": ["person-1", "person-2"]}},
        "marital_units": {
            "marital_unit-1": {"members": ["person-1"]},
            "marital_unit-2": {"members": ["person-2"]},
        },
    }


def test_a_uk_household_has_households_and_benefit_units() -> None:
    assert household_situation("uk", UK_HOUSEHOLD) == {
        "people": {"person-1": {"age": {"2026": 30}}},
        "households": {
            "household-1": {"members": ["person-1"], "region": {"2026": "WALES"}}
        },
        "benunits": {
            "benunit-1": {
                "members": ["person-1"],
                "universal_credit": {"2026": None},
            }
        },
    }


def test_a_group_without_members_keeps_an_empty_member_list() -> None:
    household = {**UK_HOUSEHOLD, "benunit": [*UK_HOUSEHOLD["benunit"]]}
    household["benunit"].append({"id": "benunit-2", "values": {}})

    situation = household_situation("uk", household)

    assert situation["benunits"]["benunit-2"] == {"members": []}


def test_the_saved_spm_choice_is_the_documents_selection_or_none() -> None:
    assert saved_spm_choice(US_HOUSEHOLD) == {
        "scenario": "ce_trend",
        "geography_kind": "national",
    }
    assert saved_spm_choice(UK_HOUSEHOLD) is None
