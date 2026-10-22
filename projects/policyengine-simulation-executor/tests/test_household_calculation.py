"""Tests for v2 household calculations, ported from the Public API's behaviour.

The first tests use a stand-in country model to cover the failure paths. The
later ones calculate with the installed US and UK models, as the Public API's
in-process calculation does; the US model takes about a minute to load once.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from uuid import UUID

import numpy as np
import pytest
from policyengine_core.errors import SituationParsingError
from policyengine_simulation_contract.household_execution import (
    HouseholdBundleProvenance,
    HouseholdExecutionFailure,
    HouseholdExecutionRequest,
    HouseholdExecutionResult,
    HouseholdWorkerIdentity,
)
from policyengine_simulation_contract.spm import SPMInputError
from policyengine_simulation_executor import household_calculation
from policyengine_simulation_executor.household_calculation import (
    CountryHouseholdModel,
    HouseholdCalculationDependencies,
    calculate_household,
    installed_household_bundle,
    load_country_household_model,
)
from policyengine_simulation_executor.household_inputs import (
    HouseholdEntity,
    HouseholdModelMetadata,
    HouseholdVariable,
)

CALCULATION_ID = UUID("00000000-0000-4000-8000-0000000000c1")
TODAY = date(2026, 6, 1)


def _worker(country: str) -> HouseholdWorkerIdentity:
    return HouseholdWorkerIdentity(
        application_name="policyengine-simulation-v2-py6-7-5",
        function_name=f"calculate_household_{country}",
    )


def _request(country: str, household: dict, **overrides) -> HouseholdExecutionRequest:
    values = {
        "calculation_id": CALCULATION_ID,
        "country": country,
        "household": household,
        "policies": [{"policy_key": "baseline", "parameters": {}}],
        "bundle": installed_household_bundle(country).model_dump(mode="json"),
    }
    values.update(overrides)
    return HouseholdExecutionRequest.model_validate(values)


class RecordingRuntime:
    """Records the spans a calculation opens, in order."""

    def __init__(self) -> None:
        self.spans: list[tuple[str, dict]] = []

    @contextmanager
    def span(self, name: str, *, attributes: dict | None = None):
        self.spans.append((name, dict(attributes or {})))
        yield


# A stand-in country model: enough of a country package to exercise the
# failure paths without importing a real one.


class StandInPopulation:
    def __init__(self, ids: list[str], count: int) -> None:
        self.ids = ids
        self.count = count

    def get_index(self, entity_id: str) -> int:
        return self.ids.index(entity_id)


class StandInSystem:
    def __init__(self, value_types: dict[str, type]) -> None:
        self.value_types = value_types
        self.variables = dict.fromkeys(value_types)

    def get_variable(self, name: str):
        return SimpleNamespace(value_type=self.value_types[name])


STAND_IN_VALUE_TYPES = {
    "age": int,
    "net_income": float,
    "spm_resources": float,
    "registration_date": date,
}


def _stand_in_model(simulation_type) -> CountryHouseholdModel:
    metadata = HouseholdModelMetadata(
        variables={
            name: HouseholdVariable(
                name,
                "person" if name == "age" else "household",
                "year",
                is_input=name == "age",
                default_value=40 if name == "age" else None,
            )
            for name in STAND_IN_VALUE_TYPES
        },
        entities={
            "person": HouseholdEntity("person", "people", frozenset()),
            "household": HouseholdEntity(
                "household", "households", frozenset({"members"})
            ),
        },
        parameters=frozenset(),
    )
    system = StandInSystem(STAND_IN_VALUE_TYPES)
    simulation_type.tax_benefit_system = system
    return CountryHouseholdModel(
        country="uk",
        simulation_type=simulation_type,
        tax_benefit_system=system,
        metadata=metadata,
    )


class CountryInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _stand_in_simulation(outcomes: dict[str, object], *, axis_points: int = 1):
    class StandInSimulation:
        tax_benefit_system: StandInSystem

        def __init__(self, *, situation: dict, **options: object) -> None:
            self.situation = situation
            self.options = options
            people = list(situation["people"])
            households = list(situation.get("households", {}))
            self.populations = {
                "people": StandInPopulation(people, len(people) * axis_points),
                "households": StandInPopulation(
                    households, len(households) * axis_points
                ),
            }

        def get_population(self, plural: str) -> StandInPopulation:
            return self.populations[plural]

        def calculate(self, variable_name: str, period: str):
            outcome = outcomes[variable_name]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    return StandInSimulation


STAND_IN_HOUSEHOLD = {
    "people": {"you": {"age": {"2026": 30}}},
    "households": {
        "home": {
            "members": ["you"],
            "net_income": {"2026": None},
            "spm_resources": {"2026": None},
        }
    },
}


def _calculate_with_stand_in(
    simulation_type, household=None, *, country="uk", **overrides
):
    model = _stand_in_model(simulation_type)
    return calculate_household(
        _request(country, household or STAND_IN_HOUSEHOLD, **overrides),
        worker=_worker(country),
        dependencies=HouseholdCalculationDependencies(
            model_loader=lambda _country: model, today=TODAY
        ),
    )


def test_a_value_that_cannot_be_calculated_is_null_without_a_warning() -> None:
    outcome = _calculate_with_stand_in(
        _stand_in_simulation(
            {"net_income": np.array([1.5]), "spm_resources": KeyError("missing")}
        )
    )

    assert isinstance(outcome, HouseholdExecutionResult)
    result = outcome.output.results[0]
    home = result.household.to_situation()["households"]["home"]
    assert home["net_income"] == {"2026": 1.5}
    assert home["spm_resources"] == {"2026": None}
    assert result.warnings == ()
    assert outcome.output.spm_config is None
    assert result.spm_provenance is None


def test_with_axes_a_failed_value_is_one_null_per_point_with_a_warning() -> None:
    household = {
        **STAND_IN_HOUSEHOLD,
        "axes": [[{"name": "age", "count": 3, "min": 0, "max": 2}]],
    }

    outcome = _calculate_with_stand_in(
        _stand_in_simulation(
            {
                "age": np.array([0, 1, 2]),
                "net_income": np.array([1.0, float("inf"), float("nan")]),
                "spm_resources": KeyError("missing"),
            },
            axis_points=3,
        ),
        household,
    )

    assert isinstance(outcome, HouseholdExecutionResult)
    result = outcome.output.results[0]
    situation = result.household.to_situation()
    assert situation["people"]["you"]["age"] == {"2026": [0, 1, 2]}
    assert situation["households"]["home"]["net_income"] == {
        "2026": [1.0, "Infinity", "NaN"]
    }
    assert situation["households"]["home"]["spm_resources"] == {
        "2026": [None, None, None]
    }
    assert situation["axes"] == household["axes"]
    assert result.warnings == (
        "Unable to calculate spm_resources for home in 2026; returned 3 null "
        "axis values: 'missing'",
    )


def test_dates_are_written_as_the_public_api_writes_them() -> None:
    # The Public API's Flask JSON encoder writes a date as an HTTP date.
    household = {
        "people": {"you": {"age": {"2026": 30}}},
        "households": {
            "home": {"members": ["you"], "registration_date": {"2026": None}}
        },
    }

    outcome = _calculate_with_stand_in(
        _stand_in_simulation(
            {"registration_date": np.array(["1970-01-01"], dtype="datetime64[D]")}
        ),
        household,
    )

    assert isinstance(outcome, HouseholdExecutionResult)
    home = outcome.output.results[0].household.to_situation()["households"]["home"]
    assert home["registration_date"] == {"2026": "Thu, 01 Jan 1970 00:00:00 GMT"}


def test_an_spm_input_error_fails_only_a_chosen_measurement(monkeypatch) -> None:
    error = CountryInputError("SPM_GEOGRAPHY_REQUIRED", "SPM needs a county")
    outcomes = {"net_income": np.array([1.0]), "spm_resources": error}
    resolved = {
        "forecast_content_sha256": "a" * 64,
        "scenario": "ce_trend",
        "geography_kind": "county",
        "geography_id": None,
        "county_vintage": "2020",
        "as_of": None,
    }
    monkeypatch.setattr(
        household_calculation,
        "resolve_household_spm_selection",
        lambda country, selection, *, sent, saved_household: dict(resolved),
    )
    monkeypatch.setattr(
        household_calculation,
        "household_spm_provenance",
        lambda simulation, expected_selection: None,
    )

    inherited = _calculate_with_stand_in(_stand_in_simulation(outcomes), country="us")
    chosen = _calculate_with_stand_in(
        _stand_in_simulation(outcomes), country="us", spm={}
    )

    assert isinstance(inherited, HouseholdExecutionResult)
    home = inherited.output.results[0].household.to_situation()["households"]["home"]
    assert home["spm_resources"] == {"2026": None}
    assert inherited.output.spm_config is not None
    assert inherited.output.spm_config.model_dump(mode="json") == resolved
    assert isinstance(chosen, HouseholdExecutionFailure)
    assert chosen.error.code == "invalid_spm_selection"
    assert chosen.error.spm_error is not None
    assert chosen.error.spm_error.code == "SPM_GEOGRAPHY_REQUIRED"
    assert chosen.error.message == "SPM needs a county"
    assert chosen.error.policy_key == "baseline"


def test_the_selection_and_its_receipt_reach_each_simulation(monkeypatch) -> None:
    resolved = {
        "forecast_content_sha256": "a" * 64,
        "scenario": "ce_trend",
        "geography_kind": "national",
        "geography_id": None,
        "county_vintage": "2020",
        "as_of": None,
    }
    created: list[dict] = []
    receipts: list[tuple[object, object]] = []

    class RecordingSimulation(_stand_in_simulation({"net_income": np.array([1.0])})):
        def __init__(self, **options: object) -> None:
            super().__init__(**options)
            created.append(self.options)

    monkeypatch.setattr(
        household_calculation,
        "resolve_household_spm_selection",
        lambda country, selection, *, sent, saved_household: resolved,
    )

    def record_receipt(simulation, expected_selection):
        receipts.append((simulation, expected_selection))

    monkeypatch.setattr(
        household_calculation, "household_spm_provenance", record_receipt
    )
    household = {
        "people": {"you": {"age": {"2026": 30}}},
        "households": {"home": {"members": ["you"], "net_income": {"2026": None}}},
    }

    _calculate_with_stand_in(RecordingSimulation, household, country="us")

    assert [options["spm"] for options in created] == [resolved]
    assert [expected for _, expected in receipts] == [resolved]


def test_an_spm_configuration_error_fails_even_an_inherited_measurement(
    monkeypatch,
) -> None:
    # Only a missing SPM input leaves an inherited measurement's value null;
    # a configuration the build cannot certify always ends the calculation.
    error = SPMInputError("SPM_CONFIGURATION_UNAVAILABLE", "no certified artifact")
    monkeypatch.setattr(
        household_calculation,
        "resolve_household_spm_selection",
        lambda country, selection, *, sent, saved_household: {
            "forecast_content_sha256": "a" * 64,
            "scenario": "ce_trend",
            "geography_kind": "national",
            "geography_id": None,
            "county_vintage": "2020",
            "as_of": None,
        },
    )

    outcome = _calculate_with_stand_in(
        _stand_in_simulation({"net_income": np.array([1.0]), "spm_resources": error}),
        country="us",
    )

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "invalid_spm_selection"
    assert outcome.error.spm_error is not None
    assert outcome.error.spm_error.code == "SPM_CONFIGURATION_UNAVAILABLE"
    assert outcome.error.message == "no certified artifact"


@pytest.mark.parametrize(
    ("simulation_error", "code", "message"),
    [
        (
            SituationParsingError(["people"], "unknown person `ghost`"),
            "invalid_household",
            "Invalid household payload: ",
        ),
        (
            RuntimeError("model exploded"),
            "calculation_failed",
            "Error calculating household under policy: model exploded",
        ),
        (
            SPMInputError("SPM_SETTINGS_INVALID", "bad SPM settings"),
            "invalid_spm_selection",
            "bad SPM settings",
        ),
    ],
)
def test_simulation_build_failures_use_public_api_messages(
    simulation_error: Exception, code: str, message: str
) -> None:
    def failing_simulation(**_: object):
        raise simulation_error

    outcome = _calculate_with_stand_in(failing_simulation)

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == code
    assert outcome.error.message.startswith(message)
    assert outcome.error.policy_key == "baseline"
    assert outcome.worker == _worker("uk")
    assert outcome.calculation_id == CALCULATION_ID


@pytest.mark.parametrize(
    ("country", "settings", "spm_code"),
    [
        ("uk", None, "SPM_SETTINGS_UNSUPPORTED"),
        ("us", None, "SPM_SETTINGS_INVALID"),
        ("us", {"geography_kind": "planet"}, "SPM_SETTINGS_INVALID"),
    ],
)
def test_spm_settings_as_sent_fail_with_public_api_codes(
    country, settings, spm_code
) -> None:
    outcome = _calculate_with_stand_in(
        _stand_in_simulation({}), country=country, spm=settings
    )

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "invalid_spm_selection"
    assert outcome.error.spm_error is not None
    assert outcome.error.spm_error.code == spm_code
    assert outcome.error.message == outcome.error.spm_error.message
    assert outcome.error.policy_key is None


def test_a_receipt_that_cannot_be_certified_is_an_spm_failure(monkeypatch) -> None:
    def uncertifiable(simulation, expected_selection):
        code = "SPM_CONFIGURATION_UNAVAILABLE"
        raise SPMInputError(code, "cannot certify")

    monkeypatch.setattr(
        household_calculation, "household_spm_provenance", uncertifiable
    )

    outcome = _calculate_with_stand_in(
        _stand_in_simulation(
            {"net_income": np.array([1.0]), "spm_resources": np.array([1.0])}
        )
    )

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "invalid_spm_selection"
    assert outcome.error.message == "cannot certify"


def test_unrecognized_inputs_are_listed_in_the_order_the_household_was_sent() -> None:
    # As the Public API lists them: it walks the household as sent, here with
    # ``axes`` first, so the axis error comes before the person's.
    household = {
        "axes": [[{"name": "not_an_axis_variable", "count": 2, "min": 0, "max": 1}]],
        "people": {"you": {"age": {"2026": 30}, "not_a_variable": {"2026": 1}}},
        "households": {"home": {"members": ["you"], "net_income": {"2026": None}}},
    }

    outcome = _calculate_with_stand_in(_stand_in_simulation({}), household)

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "invalid_inputs"
    assert [error.path for error in outcome.error.input_errors] == [
        "household.axes[0][0].name",
        "household.people.you.not_a_variable",
    ]
    assert outcome.error.message == (
        "Unrecognized calculate input(s): Unrecognized household axis variable "
        "`not_an_axis_variable` at `household.axes[0][0].name`.; Unrecognized "
        "household variable `not_a_variable` at "
        "`household.people.you.not_a_variable`."
    )


def test_a_request_for_another_bundle_is_refused() -> None:
    bundle = installed_household_bundle("uk").model_copy(
        update={"policyengine_version": "0.0.1"}
    )

    outcome = _calculate_with_stand_in(
        _stand_in_simulation({}), bundle=bundle.model_dump(mode="json")
    )

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "bundle_mismatch"
    assert outcome.error.policy_key is None


def test_a_function_refuses_another_countrys_household() -> None:
    model = _stand_in_model(_stand_in_simulation({}))

    with pytest.raises(ValueError, match="calculates us households only"):
        calculate_household(
            _request("uk", STAND_IN_HOUSEHOLD),
            worker=_worker("us"),
            required_country="us",
            dependencies=HouseholdCalculationDependencies(
                model_loader=lambda _country: model
            ),
        )


def test_the_installed_bundle_names_the_country_package() -> None:
    bundle = installed_household_bundle("us")

    assert isinstance(bundle, HouseholdBundleProvenance)
    assert bundle.country_package_name == "policyengine-us"


# Installed country models: real calculations, compared with the Public API's
# result shape.

US_FAMILY = {
    "people": {
        "you": {"age": {"2026": 35}, "employment_income": {"2026": 30_000}},
        "your first dependent": {"age": {"2026": 5}},
    },
    "tax_units": {
        "your tax unit": {
            "members": ["you", "your first dependent"],
            "ctc": {"2026": None},
        }
    },
    "spm_units": {"your spm unit": {"members": ["you", "your first dependent"]}},
    "families": {"your family": {"members": ["you", "your first dependent"]}},
    "marital_units": {
        "your marital unit": {"members": ["you"]},
        "your first dependent's marital unit": {"members": ["your first dependent"]},
    },
    "households": {
        "your household": {
            "members": ["you", "your first dependent"],
            "state_name": {"2026": "TX"},
            "county_fips": {"2026": "48201"},
            "household_net_income": {"2026": None},
        }
    },
}
CTC_REFORM = {
    "gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01.2100-12-31": 5_000}
}


def test_us_baseline_and_reform_are_calculated_in_one_request() -> None:
    runtime = RecordingRuntime()
    request = _request(
        "us",
        US_FAMILY,
        policies=[
            {"policy_key": "baseline", "parameters": {}},
            {"policy_key": "reform", "parameters": CTC_REFORM},
        ],
    )

    outcome = calculate_household(
        request,
        worker=_worker("us"),
        dependencies=HouseholdCalculationDependencies(today=TODAY, runtime=runtime),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    assert outcome.calculation_id == CALCULATION_ID
    assert outcome.output.bundle == installed_household_bundle("us")
    baseline, reform = outcome.output.results
    assert (baseline.policy_key, reform.policy_key) == ("baseline", "reform")
    baseline_situation = baseline.household.to_situation()
    reform_situation = reform.household.to_situation()
    # Same structure as the input, with each requested value filled in.
    assert list(baseline_situation) == list(US_FAMILY)
    assert baseline_situation["people"] == US_FAMILY["people"]
    baseline_ctc = baseline_situation["tax_units"]["your tax unit"]["ctc"]["2026"]
    reform_ctc = reform_situation["tax_units"]["your tax unit"]["ctc"]["2026"]
    assert isinstance(baseline_ctc, float)
    assert isinstance(reform_ctc, float)
    assert reform_ctc > baseline_ctc
    # A US household inherits the certified SPM selection and carries its receipt.
    assert outcome.output.spm_config is not None
    assert outcome.output.spm_config.geography_kind == "county"
    assert baseline.spm_provenance is not None
    assert reform.spm_provenance is not None
    assert baseline.spm_provenance.scenario == outcome.output.spm_config.scenario
    assert [name for name, _ in runtime.spans] == [
        "v2_household_country_model_load",
        "v2_household_input_normalization",
        "v2_household_calculation",
        "v2_household_calculation",
    ]
    assert runtime.spans[3][1] == {"country": "us", "policy_key": "reform"}


def test_us_axes_calculate_one_value_per_point() -> None:
    household = {
        **US_FAMILY,
        "axes": [
            [
                {
                    "name": "employment_income",
                    "count": 5,
                    "min": 0,
                    "max": 80_000,
                    "period": "2026",
                }
            ]
        ],
    }

    outcome = calculate_household(
        _request("us", household),
        worker=_worker("us"),
        dependencies=HouseholdCalculationDependencies(today=TODAY),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    situation = outcome.output.results[0].household.to_situation()
    incomes = situation["households"]["your household"]["household_net_income"]
    assert len(incomes["2026"]) == 5
    assert all(isinstance(income, float) for income in incomes["2026"])
    assert incomes["2026"][0] < incomes["2026"][4]
    # With axes, every value of a known variable is calculated at each point,
    # and the people's provided values are the axis values.
    assert situation["people"]["you"]["employment_income"] == {
        "2026": [0.0, 20_000.0, 40_000.0, 60_000.0, 80_000.0]
    }
    assert situation["people"]["you"]["age"] == {"2026": [35.0] * 5}
    assert situation["households"]["your household"]["state_name"] == {
        "2026": ["TX"] * 5
    }
    assert situation["axes"] == household["axes"]


def test_us_calculate_full_adds_and_calculates_every_missing_variable() -> None:
    outcome = calculate_household(
        _request("us", US_FAMILY, add_missing_variables=True),
        worker=_worker("us"),
        dependencies=HouseholdCalculationDependencies(today=TODAY),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    situation = outcome.output.results[0].household.to_situation()
    you = situation["people"]["you"]
    tax_unit = situation["tax_units"]["your tax unit"]
    model = load_country_household_model("us")
    person_variables = {
        variable.name
        for variable in model.metadata.variables.values()
        if variable.entity == "person"
        and variable.definition_period in {"year", "month", "eternity"}
    }
    assert person_variables <= set(you)
    assert isinstance(you["is_blind"]["2026"], bool)
    assert isinstance(tax_unit["income_tax"]["2026"], float)
    assert isinstance(tax_unit["eitc"]["2026"], float)
    assert isinstance(
        situation["households"]["your household"]["household_net_income"]["2026"],
        float,
    )


UK_SINGLE = {
    "people": {"you": {"age": {"2026": 40}, "employment_income": {"2026": 30_000}}},
    "benunits": {"your benefit unit": {"members": ["you"]}},
    "households": {
        "your household": {"members": ["you"], "household_net_income": {"2026": None}}
    },
}


def test_uk_axes_calculate_one_value_per_point() -> None:
    household = {
        **UK_SINGLE,
        "axes": [
            [
                {
                    "name": "employment_income",
                    "count": 3,
                    "min": 0,
                    "max": 60_000,
                    "period": "2026",
                }
            ]
        ],
    }

    outcome = calculate_household(
        _request("uk", household),
        worker=_worker("uk"),
        dependencies=HouseholdCalculationDependencies(today=TODAY),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    situation = outcome.output.results[0].household.to_situation()
    incomes = situation["households"]["your household"]["household_net_income"]
    assert len(incomes["2026"]) == 3
    assert incomes["2026"][0] < incomes["2026"][2]
    assert situation["people"]["you"]["employment_income"] == {
        "2026": [0.0, 30_000.0, 60_000.0]
    }
    assert outcome.output.results[0].spm_provenance is None
    assert outcome.output.spm_config is None


def test_uk_calculate_full_adds_every_missing_yearly_variable() -> None:
    outcome = calculate_household(
        _request("uk", UK_SINGLE, add_missing_variables=True),
        worker=_worker("uk"),
        dependencies=HouseholdCalculationDependencies(today=TODAY),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    you = outcome.output.results[0].household.to_situation()["people"]["you"]
    assert isinstance(you["income_tax"]["2026"], float)


def test_uk_unrecognized_inputs_fail_with_public_api_errors() -> None:
    household = {
        **UK_SINGLE,
        "people": {"you": {**UK_SINGLE["people"]["you"], "not_a_variable": 1}},
    }
    request = _request(
        "uk",
        household,
        policies=[
            {"policy_key": "baseline", "parameters": {}},
            {"policy_key": "reform", "parameters": {"gov.not.a.parameter": {}}},
        ],
    )

    outcome = calculate_household(
        request,
        worker=_worker("uk"),
        dependencies=HouseholdCalculationDependencies(today=TODAY),
    )

    # The first policy's request already fails on the household's own input,
    # as the Public API's baseline request would.
    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "invalid_inputs"
    assert outcome.error.message == (
        "Unrecognized calculate input(s): Unrecognized household variable "
        "`not_a_variable` at `household.people.you.not_a_variable`."
    )
    assert outcome.error.policy_key is None
    assert [error.path for error in outcome.error.input_errors] == [
        "household.people.you.not_a_variable"
    ]


def test_uk_unrecognized_policy_parameters_name_their_policy() -> None:
    request = _request(
        "uk",
        UK_SINGLE,
        policies=[
            {"policy_key": "baseline", "parameters": {}},
            {"policy_key": "reform", "parameters": {"gov.not.a.parameter": {}}},
        ],
    )

    outcome = calculate_household(
        request,
        worker=_worker("uk"),
        dependencies=HouseholdCalculationDependencies(today=TODAY),
    )

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "invalid_inputs"
    assert outcome.error.policy_key == "reform"
    assert [error.path for error in outcome.error.input_errors] == [
        "policy.gov.not.a.parameter"
    ]


def test_uk_reform_values_given_as_text_are_converted_to_numbers() -> None:
    # The Public API converts a reform value to the parameter's type, reading
    # numeric text as a number first.
    basic_rate = "gov.hmrc.income_tax.rates.uk[0].rate"
    request = _request(
        "uk",
        UK_SINGLE,
        policies=[
            {"policy_key": "baseline", "parameters": {}},
            {
                "policy_key": "reform",
                "parameters": {basic_rate: {"2026-01-01.2100-12-31": "0.3"}},
            },
        ],
    )

    outcome = calculate_household(
        request,
        worker=_worker("uk"),
        dependencies=HouseholdCalculationDependencies(today=TODAY),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    baseline, reform = (
        result.household.to_situation()["households"]["your household"][
            "household_net_income"
        ]["2026"]
        for result in outcome.output.results
    )
    assert isinstance(baseline, float)
    assert isinstance(reform, float)
    assert reform < baseline


def test_uk_deprecated_inputs_warn_in_every_policy_result() -> None:
    household = {
        **UK_SINGLE,
        "people": {
            "you": {
                **UK_SINGLE["people"]["you"],
                "uc_has_limited_capability_for_work": {"2026": True},
            }
        },
    }
    request = _request(
        "uk",
        household,
        policies=[
            {"policy_key": "baseline", "parameters": {}},
            {"policy_key": "other-baseline", "parameters": {}},
        ],
    )

    outcome = calculate_household(
        request,
        worker=_worker("uk"),
        dependencies=HouseholdCalculationDependencies(today=TODAY),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    for result in outcome.output.results:
        assert len(result.warnings) == 1
        assert "uc_has_limited_capability_for_work" in result.warnings[0]
        you = result.household.to_situation()["people"]["you"]
        assert "uc_has_limited_capability_for_work" not in you


def test_uk_spm_settings_are_refused() -> None:
    outcome = calculate_household(
        _request("uk", UK_SINGLE, spm={}),
        worker=_worker("uk"),
        dependencies=HouseholdCalculationDependencies(today=TODAY),
    )

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "invalid_spm_selection"
    assert outcome.error.spm_error is not None
    assert outcome.error.spm_error.code == "SPM_SETTINGS_UNSUPPORTED"


def test_uk_member_who_is_not_a_person_is_an_invalid_household() -> None:
    household = {
        **UK_SINGLE,
        "benunits": {"your benefit unit": {"members": ["you", "ghost"]}},
    }

    outcome = calculate_household(
        _request("uk", household),
        worker=_worker("uk"),
        dependencies=HouseholdCalculationDependencies(today=TODAY),
    )

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "invalid_household"
    assert outcome.error.message.startswith("Invalid household payload: ")


def test_country_models_are_loaded_once_per_process() -> None:
    first = load_country_household_model("uk")

    assert load_country_household_model("uk") is first
    assert first.metadata.entities["benunit"].plural == "benunits"
    assert "gov.hmrc.income_tax.rates.uk[0].rate" in first.metadata.parameters
