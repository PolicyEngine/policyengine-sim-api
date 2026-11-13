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
    EconomySimulationDefinition,
    SimulationChoice,
    canonical_json,
    definition_key,
)

REFORM_POLICY_ID = UUID("11111111-2222-3333-4444-555555555555")
DYNAMIC_ID = UUID("99999999-8888-7777-6666-555555555555")
CURRENT_LAW = SimulationChoice(policy_id=None, dynamic_id=None)
REFORM_P = SimulationChoice(policy_id=REFORM_POLICY_ID, dynamic_id=None)
NATIONAL_DATASET = "microcosm_uk_2024_25"
LOCAL_AREA_DATASET = "microcosm_uk_2024_25_dense"
NATIONAL_SPM = {"geography_kind": "national"}


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
        spm=None,
    )
    return replace(report, **changes)


def _reform_p(**changes: Any) -> EconomySimulationDefinition:
    """Return report N's reform simulation."""
    simulation = EconomySimulationDefinition(
        country="uk",
        choice=REFORM_P,
        dataset_name=NATIONAL_DATASET,
        region_code="uk",
        year=2026,
        spm=None,
    )
    return replace(simulation, **changes)


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


def test_a_simulation_key_object_holds_everything_that_changes_its_numbers() -> None:
    """Answer 1a, and 4c: the SPM selection, as given or absent, is in it."""
    assert KEY_VERSION == 1
    assert _reform_p().key_object() == {
        "country": "uk",
        "dataset": NATIONAL_DATASET,
        "dynamic_id": None,
        "filter": None,
        "key_version": 1,
        "policy_id": REFORM_POLICY_ID,
        "record": "simulation",
        "region": "uk",
        "simulation_type": "economy",
        "spm": None,
        "year": 2026,
    }
    assert _reform_p().definition_key[:16] == "b989f3178d09e525"


def test_each_part_of_a_simulation_definition_changes_its_key() -> None:
    variants = [
        _reform_p(),
        _reform_p(country="us"),
        _reform_p(choice=CURRENT_LAW),
        _reform_p(
            choice=SimulationChoice(policy_id=REFORM_POLICY_ID, dynamic_id=DYNAMIC_ID)
        ),
        _reform_p(dataset_name=LOCAL_AREA_DATASET),
        _reform_p(region_code="country/wales"),
        _reform_p(year=2027),
        _reform_p(spm=NATIONAL_SPM),
        _reform_p(spm={}),
    ]

    assert len({variant.definition_key for variant in variants}) == len(variants)


def test_a_report_key_object_names_its_sides_and_what_they_share() -> None:
    """Answer 4: a report has no options; the SPM selection is named once."""
    assert _report_n().key_object() == {
        "baseline": {"dynamic_id": None, "policy_id": None},
        "budget_window_size": None,
        "country": "uk",
        "dataset": NATIONAL_DATASET,
        "key_version": 1,
        "record": "report",
        "reform": {"dynamic_id": None, "policy_id": REFORM_POLICY_ID},
        "region": "uk",
        "report_type": "economy",
        "spm": None,
        "year": 2026,
    }
    assert _report_n().definition_key[:16] == "1072f5d1cc18140f"


def test_each_part_of_a_report_definition_changes_its_key() -> None:
    variants = [
        _report_n(),
        _report_n(country="us"),
        _report_n(dataset_name=LOCAL_AREA_DATASET),
        _report_n(region_code="country/wales"),
        _report_n(year=2027),
        _report_n(budget_window_size=10),
        _report_n(spm=NATIONAL_SPM),
        _report_n(baseline=REFORM_P, reform=CURRENT_LAW),
        _report_n(
            reform=SimulationChoice(policy_id=REFORM_POLICY_ID, dynamic_id=DYNAMIC_ID)
        ),
        _report_n(baseline=SimulationChoice(policy_id=None, dynamic_id=DYNAMIC_ID)),
    ]

    assert len({variant.definition_key for variant in variants}) == len(variants)


def test_a_reports_sides_are_the_simulations_its_definition_names() -> None:
    """Each side shares the report's dataset, region, year and SPM selection."""
    report = _report_n(spm=NATIONAL_SPM)

    assert report.simulation("reform") == _reform_p(spm=NATIONAL_SPM)
    assert report.simulation("baseline") == _reform_p(
        choice=CURRENT_LAW, spm=NATIONAL_SPM
    )
    # Current law against current law names one simulation on both sides.
    same_policy = _report_n(reform=CURRENT_LAW)
    assert (
        same_policy.simulation("baseline").definition_key
        == same_policy.simulation("reform").definition_key
    )


def test_a_budget_window_years_simulations_are_of_that_year() -> None:
    window = _report_n(budget_window_size=3)

    assert window.simulation("reform", year=2027) == _reform_p(year=2027)


def test_the_dataset_independent_key_is_the_report_key_without_its_dataset() -> None:
    """Revised answer 1a and answer 4: the report key holds no options."""
    report_n = _report_n()
    expected = {
        name: value
        for name, value in report_n.key_object().items()
        if name != "dataset"
    }
    expected["record"] = "report_dataset_independent"

    assert report_n.dataset_independent_key_object() == expected
    assert report_n.dataset_independent_key == definition_key(expected)
    assert report_n.dataset_independent_key[:16] == "b2d140ad0201ada4"


def test_reports_differing_only_in_their_dataset_share_that_key() -> None:
    national = _report_n()
    local_area = _report_n(dataset_name=LOCAL_AREA_DATASET)
    other_spm = _report_n(dataset_name=LOCAL_AREA_DATASET, spm=NATIONAL_SPM)
    other_region = _report_n(
        dataset_name=LOCAL_AREA_DATASET, region_code="country/wales"
    )

    assert national.dataset_independent_key == local_area.dataset_independent_key
    assert national.definition_key != local_area.definition_key
    assert other_spm.dataset_independent_key != national.dataset_independent_key
    assert other_region.dataset_independent_key != national.dataset_independent_key
