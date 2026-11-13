"""Definition keys: the canonical JSON of what identifies a record, hashed."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from policyengine_simulation_persistence import (
    KEY_VERSION,
    EconomyReportDefinition,
    SimulationChoice,
    canonical_json,
    definition_key,
)

REFORM_POLICY_ID = UUID("11111111-2222-3333-4444-555555555555")
CURRENT_LAW = SimulationChoice(policy_id=None, dynamic_id=None)
REFORM_P = SimulationChoice(policy_id=REFORM_POLICY_ID, dynamic_id=None)
NATIONAL_DATASET = "microcosm_uk_2024_25"
LOCAL_AREA_DATASET = "microcosm_uk_2024_25_dense"


def _report_n(**changes: Any) -> EconomyReportDefinition:
    """Return the proposal's report N: current law against reform P, UK, 2026."""
    report = EconomyReportDefinition(
        country="uk",
        baseline=CURRENT_LAW,
        reform=REFORM_P,
        dataset_name=NATIONAL_DATASET,
        region_code="uk",
        year=2026,
        budget_window_size=None,
        options={"include_cliffs": False},
    )
    return replace(report, **changes)


def test_the_canonical_json_sorts_keys_with_no_spaces_and_keeps_utf_8() -> None:
    definition = {
        "region": "country/wales",
        "label": "Région",
        "policy_id": UUID("AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"),
        "missing": None,
        "nested": {"b": 2, "a": [1, True]},
    }

    assert canonical_json(definition) == (
        '{"label":"Région","missing":null,"nested":{"a":[1,true],"b":2},'
        '"policy_id":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee","region":"country/wales"}'
    )


def test_a_key_is_the_lowercase_sha_256_of_the_canonical_json() -> None:
    definition = {"record": "report", "label": "Région"}
    expected = hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()

    key = definition_key(definition)

    assert key == expected
    assert len(key) == 64
    assert key == key.lower()


def test_a_number_json_cannot_write_exactly_is_refused() -> None:
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json({"value": float("nan")})


def test_a_value_json_has_no_form_for_is_refused() -> None:
    with pytest.raises(TypeError, match="set"):
        canonical_json({"value": {1, 2}})


def test_the_proposals_worked_example_reports_have_its_keys() -> None:
    """N, N2 and L of the proposal's worked example, by their first 16 digits."""
    report_n = _report_n()
    report_n2 = _report_n(options={"include_cliffs": True})
    report_l = _report_n(dataset_name=LOCAL_AREA_DATASET, options={})

    assert report_n.definition_key[:16] == "4bb1299756cb0a89"
    assert report_n2.definition_key[:16] == "bb74ac904ed3a1c8"
    assert report_l.definition_key[:16] == "907455a2702b0559"


def test_a_report_key_object_names_its_version_and_record() -> None:
    assert KEY_VERSION == 1
    assert _report_n().key_object() == {
        "baseline": {"dynamic_id": None, "policy_id": None},
        "budget_window_size": None,
        "country": "uk",
        "dataset": NATIONAL_DATASET,
        "key_version": 1,
        "options": {"include_cliffs": False},
        "record": "report",
        "reform": {"dynamic_id": None, "policy_id": REFORM_POLICY_ID},
        "region": "uk",
        "report_type": "economy",
        "year": 2026,
    }


def test_each_part_of_a_report_definition_changes_its_key() -> None:
    dynamic_id = UUID("99999999-8888-7777-6666-555555555555")
    variants = [
        _report_n(),
        _report_n(country="us"),
        _report_n(dataset_name=LOCAL_AREA_DATASET),
        _report_n(region_code="country/wales"),
        _report_n(year=2027),
        _report_n(budget_window_size=10),
        _report_n(options={"include_cliffs": True}),
        _report_n(options={"include_cliffs": False, "spm": {"scenario": "low"}}),
        _report_n(baseline=REFORM_P, reform=CURRENT_LAW),
        _report_n(
            reform=SimulationChoice(policy_id=REFORM_POLICY_ID, dynamic_id=dynamic_id)
        ),
        _report_n(baseline=SimulationChoice(policy_id=None, dynamic_id=dynamic_id)),
    ]

    assert len({variant.definition_key for variant in variants}) == len(variants)
