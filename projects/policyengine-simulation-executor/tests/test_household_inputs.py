"""Tests for household input preparation, ported from the Public API."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from policyengine_simulation_executor.household_inputs import (
    HouseholdEntity,
    HouseholdModelMetadata,
    HouseholdVariable,
    add_missing_variables,
    drop_deprecated_inputs,
    find_unrecognized_household_inputs,
    find_unrecognized_policy_inputs,
    household_year,
    requested_computations,
    unrecognized_inputs_message,
)

TODAY = date(2027, 3, 1)


def _metadata() -> HouseholdModelMetadata:
    variables = [
        HouseholdVariable("age", "person", "year", is_input=True, default_value=40),
        HouseholdVariable(
            "employment_income", "person", "year", is_input=True, default_value=0
        ),
        HouseholdVariable(
            "is_blind", "person", "eternity", is_input=True, default_value=False
        ),
        HouseholdVariable(
            "monthly_rent", "household", "month", is_input=True, default_value=0
        ),
        HouseholdVariable(
            "state_name", "household", "year", is_input=True, default_value="CA"
        ),
        HouseholdVariable(
            "household_net_income",
            "household",
            "year",
            is_input=False,
            default_value=0,
        ),
        HouseholdVariable(
            "daily_amount", "household", "day", is_input=True, default_value=0
        ),
    ]
    return HouseholdModelMetadata(
        variables={variable.name: variable for variable in variables},
        entities={
            "person": HouseholdEntity("person", "people", frozenset()),
            "household": HouseholdEntity(
                "household", "households", frozenset({"members"})
            ),
        },
        parameters=frozenset({"gov.tax.rate", "gov.tax.brackets[0].rate"}),
    )


def _situation() -> dict:
    return {
        "people": {"you": {"age": {"2026": 30}}, "partner": {}},
        "households": {"home": {"members": ["you", "partner"]}},
    }


def test_household_year_is_the_first_age_period_of_you() -> None:
    assert household_year(_situation(), today=TODAY) == "2026"
    assert household_year({"people": {"partner": {}}}, today=TODAY) == "2027"
    assert household_year({"people": {"you": {"age": 30}}}, today=TODAY) == "2027"


def test_add_missing_variables_matches_the_public_api() -> None:
    situation = _situation()

    add_missing_variables(situation, _metadata(), today=TODAY)

    assert situation == {
        "people": {
            "you": {
                "age": {"2026": 30},
                "employment_income": {"2026": 0},
                "is_blind": {"2026": False},
            },
            "partner": {
                "age": {"2026": 40},
                "employment_income": {"2026": 0},
                "is_blind": {"2026": False},
            },
        },
        "households": {
            "home": {
                "members": ["you", "partner"],
                "monthly_rent": {"2026": 0},
                "state_name": {"2026": "CA"},
                # Calculated variables are requested, never defaulted.
                "household_net_income": {"2026": None},
            }
        },
    }


def test_deprecated_inputs_are_dropped_with_public_api_warnings() -> None:
    situation = {
        "people": {"you": {"age": {"2026": 30}, "medical_out_of_pocket_expenses": 5}},
        "axes": [
            [{"name": "uc_has_limited_capability_for_work", "count": 2}],
            [{"name": "employment_income", "count": 2}],
        ],
    }

    result = drop_deprecated_inputs(situation)

    assert result.household == {
        "people": {"you": {"age": {"2026": 30}}},
        "axes": [[{"name": "employment_income", "count": 2}]],
    }
    assert "medical_out_of_pocket_expenses" in situation["people"]["you"]
    assert [warning.message for warning in result.warnings] == [
        "Input `medical_out_of_pocket_expenses` on `people/you` is deprecated and "
        "was ignored for this calculation. Removed in policyengine-us 1.673.0. "
        "Migrate non-premium spending to `other_medical_expenses` and premium "
        "spending to `health_insurance_premiums`.",
        "Input `uc_has_limited_capability_for_work` on `axes[0][0].name` is "
        "deprecated and was ignored for this calculation. Removed in "
        "policyengine-uk 2.129.0. Set `uc_limited_capability_for_work` instead.",
    ]


def test_axes_left_empty_by_deprecated_inputs_are_removed() -> None:
    situation = {
        "people": {"you": {}},
        "axes": [[{"name": "medical_out_of_pocket_expenses", "count": 2}]],
    }

    assert "axes" not in drop_deprecated_inputs(situation).household


def test_unrecognized_household_inputs_use_public_api_types_and_messages() -> None:
    situation = {
        "people": {
            "you": {"age": {"2026": 30}, "not_a_variable": 1, "state_name": "CA"}
        },
        "households": {"home": {"members": ["you"]}},
        "pets": {"rex": {}},
        "axes": [[{"name": "unknown_axis", "count": 2}]],
    }

    errors = find_unrecognized_household_inputs(situation, _metadata())

    assert [error.model_dump(mode="json") for error in errors] == [
        {
            "type": "household_variable",
            "name": "not_a_variable",
            "path": "household.people.you.not_a_variable",
            "message": (
                "Unrecognized household variable `not_a_variable` at "
                "`household.people.you.not_a_variable`."
            ),
        },
        {
            "type": "household_variable_wrong_entity",
            "name": "state_name",
            "path": "household.people.you.state_name",
            "message": (
                "Household variable `state_name` belongs on `households`, not "
                "`people`, at `household.people.you.state_name`."
            ),
            "expected_entity_plural": "households",
            "actual_entity_plural": "people",
        },
        {
            "type": "household_entity",
            "name": "pets",
            "path": "household.pets",
            "message": (
                "Unrecognized household entity group `pets` at `household.pets`."
            ),
        },
        {
            "type": "household_axis_variable",
            "name": "unknown_axis",
            "path": "household.axes[0][0].name",
            "message": (
                "Unrecognized household axis variable `unknown_axis` at "
                "`household.axes[0][0].name`."
            ),
        },
    ]
    assert unrecognized_inputs_message(errors[:1]) == (
        "Unrecognized calculate input(s): Unrecognized household variable "
        "`not_a_variable` at `household.people.you.not_a_variable`."
    )


def test_unrecognized_policy_parameters_are_reported_by_path() -> None:
    errors = find_unrecognized_policy_inputs(
        {"gov.tax.rate": {}, "gov.tax.brackets[0].rate": {}, "gov.missing": {}},
        _metadata(),
    )

    assert [error.model_dump(mode="json") for error in errors] == [
        {
            "type": "policy_parameter",
            "name": "gov.missing",
            "path": "policy.gov.missing",
            "message": "Unrecognized policy parameter `gov.missing` at "
            "`policy.gov.missing`.",
        }
    ]


def test_deprecated_inputs_leave_the_callers_household_unchanged() -> None:
    situation = {"people": {"you": {"medical_out_of_pocket_expenses": 5}}}

    result = drop_deprecated_inputs(situation)

    assert result.household == {"people": {"you": {}}}
    assert situation == {"people": {"you": {"medical_out_of_pocket_expenses": 5}}}


def test_a_household_without_deprecated_inputs_keeps_its_axes() -> None:
    situation = {
        "people": {"you": {}},
        "axes": [[{"name": "employment_income", "count": 2}]],
    }

    result = drop_deprecated_inputs(situation)

    assert result.household == situation
    assert result.warnings == ()


def test_requested_computations_are_the_null_values_without_axes() -> None:
    situation = {
        "people": {"you": {"age": {"2026": 30}, "employment_income": {"2026": None}}},
        "households": {
            "home": {"members": ["you"], "household_net_income": {"2026": None}}
        },
    }

    assert requested_computations(situation, include_provided_values=False) == [
        ("people", "you", "employment_income", "2026"),
        ("households", "home", "household_net_income", "2026"),
    ]


def test_requested_computations_with_axes_cover_every_known_variable() -> None:
    situation = {
        "people": {"you": {"age": {"2026": 30}, "unknown": {"2026": 1}}},
        "households": {"home": {"members": ["you"]}},
        "axes": [[{"name": "age", "count": 2, "min": 0, "max": 1}]],
    }

    assert requested_computations(
        situation,
        include_provided_values=True,
        variable_names={"age", "employment_income"},
    ) == [("people", "you", "age", "2026")]


def test_metadata_is_read_from_a_tax_benefit_system() -> None:
    class Enum:
        pass

    class Level:
        name = "LOW"

    person = SimpleNamespace(key="person", plural="people", is_person=True)
    household = SimpleNamespace(
        key="household",
        plural="households",
        is_person=False,
        roles=[SimpleNamespace(plural="members"), SimpleNamespace(plural=None)],
    )

    def variable(name, entity, value_type, default_value, *, is_input=True):
        return SimpleNamespace(
            entity=entity,
            definition_period="year",
            value_type=value_type,
            default_value=default_value,
            is_input_variable=lambda: is_input,
        )

    parameters = SimpleNamespace(
        get_descendants=lambda: [
            SimpleNamespace(name="gov.tax.rate"),
            SimpleNamespace(name="calibration.factor"),
        ]
    )
    system = SimpleNamespace(
        entities=[person, household],
        variables={
            "age": variable("age", person, int, 40),
            "level": variable("level", household, Enum, Level()),
            "label": variable("label", household, str, "x"),
            "start": variable("start", person, date, date(2020, 1, 1)),
            "net_income": variable("net_income", household, float, 0.0, is_input=False),
        },
        parameters=parameters,
    )

    metadata = HouseholdModelMetadata.from_tax_benefit_system(system)

    assert metadata.entities["household"] == HouseholdEntity(
        "household", "households", frozenset({"members"})
    )
    assert metadata.entities["person"].role_plurals == frozenset()
    assert [
        (item.name, item.entity, item.default_value, item.is_input)
        for item in metadata.variables.values()
    ] == [
        ("age", "person", 40, True),
        ("level", "household", "LOW", True),
        ("label", "household", None, True),
        ("start", "person", None, True),
        ("net_income", "household", 0.0, False),
    ]
    assert metadata.parameters == frozenset({"gov.tax.rate"})
