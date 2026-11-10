"""Tests for v2 household calculations, ported from the Public API's behaviour.

The first tests use a stand-in country model to cover the failure paths. The
later ones calculate with the installed US and UK models, as the Public API's
in-process calculation does; the US model takes about a minute to load once.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime
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


def _worker(country: str) -> HouseholdWorkerIdentity:
    return HouseholdWorkerIdentity(
        application_name="policyengine-simulation-v2-py6-7-5",
        function_name=f"calculate_household_{country}",
    )


def _request(country: str, household: dict, **overrides) -> HouseholdExecutionRequest:
    values = {
        "calculation_id": CALCULATION_ID,
        "country": country,
        "year": "2026",
        "household": household,
        "policy": {"parameters": {}},
        "bundle": installed_household_bundle(country).model_dump(mode="json"),
    }
    values.update(overrides)
    return HouseholdExecutionRequest.model_validate(values)


class RecordingRuntime:
    """Records the spans a calculation opens, in order, and which are open."""

    def __init__(self) -> None:
        self.spans: list[tuple[str, dict]] = []
        self.open_spans: list[str] = []

    @contextmanager
    def span(self, name: str, *, attributes: dict | None = None):
        self.spans.append((name, dict(attributes or {})))
        self.open_spans.append(name)
        try:
            yield
        finally:
            self.open_spans.pop()


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
            model_loader=lambda _country: model
        ),
    )


def test_a_value_that_cannot_be_calculated_is_null_without_a_warning() -> None:
    outcome = _calculate_with_stand_in(
        _stand_in_simulation(
            {"net_income": np.array([1.5]), "spm_resources": KeyError("missing")}
        )
    )

    assert isinstance(outcome, HouseholdExecutionResult)
    result = outcome.output
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
    result = outcome.output
    situation = result.household.to_situation()
    assert situation["people"]["you"]["age"] == {"2026": [0, 1, 2]}
    assert situation["households"]["home"]["net_income"] == {
        "2026": [1.0, "Infinity", "NaN"]
    }
    assert situation["households"]["home"]["spm_resources"] == {
        "2026": [None, None, None]
    }
    # The axis named no period, so it varied the request's year.
    assert situation["axes"] == [[{**household["axes"][0][0], "period": "2026"}]]
    assert result.warnings == (
        "Unable to calculate spm_resources for home in 2026; returned 3 null "
        "axis values: 'missing'",
    )


def test_single_values_are_calculated_for_the_request_year() -> None:
    # A value given once, not by period, is for the request's year, and a
    # single null asks for that year's value, which the Public API leaves
    # uncalculated.
    created: list[dict] = []

    class RecordingSimulation(_stand_in_simulation({"net_income": np.array([2.5])})):
        def __init__(self, **options: object) -> None:
            super().__init__(**options)
            created.append(self.situation)

    household = {
        "people": {"you": {"age": 30}},
        "households": {"home": {"members": ["you"], "net_income": None}},
    }

    outcome = _calculate_with_stand_in(RecordingSimulation, household, year="2024")

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    assert created[0]["people"]["you"]["age"] == {"2024": 30}
    situation = outcome.output.household.to_situation()
    assert situation["people"]["you"]["age"] == {"2024": 30}
    assert situation["households"]["home"]["net_income"] == {"2024": 2.5}


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
    home = outcome.output.household.to_situation()["households"]["home"]
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
    home = inherited.output.household.to_situation()["households"]["home"]
    assert home["spm_resources"] == {"2026": None}
    assert inherited.output.spm_config is not None
    assert inherited.output.spm_config.model_dump(mode="json") == resolved
    assert isinstance(chosen, HouseholdExecutionFailure)
    assert chosen.error.code == "invalid_spm_selection"
    assert chosen.error.spm_error is not None
    assert chosen.error.spm_error.code == "SPM_GEOGRAPHY_REQUIRED"
    assert chosen.error.message == "SPM needs a county"


def test_the_selection_and_its_receipt_reach_the_simulation(monkeypatch) -> None:
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


@pytest.mark.parametrize(
    ("source", "saved_household"), [("request", False), ("saved_household", True)]
)
def test_spm_settings_are_resolved_while_preparing_the_household(
    monkeypatch, source, saved_household
) -> None:
    # The country model loads first, under its own span, so the time the US
    # model takes to import is measured there and not in SPM resolution.
    runtime = RecordingRuntime()
    events: list[tuple[str, object]] = []
    model = _stand_in_model(_stand_in_simulation({"net_income": np.array([1.0])}))

    def load_model(country):
        events.append(("model", list(runtime.open_spans)))
        return model

    def resolve(country, selection, *, sent, saved_household):
        events.append(("spm", list(runtime.open_spans)))
        events.append(("spm options", (selection, sent, saved_household)))

    monkeypatch.setattr(
        household_calculation, "resolve_household_spm_selection", resolve
    )
    household = {
        "people": {"you": {"age": {"2026": 30}}},
        "households": {"home": {"members": ["you"], "net_income": {"2026": None}}},
    }

    outcome = calculate_household(
        _request("us", household, spm={}, spm_selection_source=source),
        worker=_worker("us"),
        dependencies=HouseholdCalculationDependencies(
            model_loader=load_model, runtime=runtime
        ),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    assert events == [
        ("model", ["v2_household_country_model_load"]),
        ("spm", ["v2_household_input_normalization"]),
        ("spm options", ({}, True, saved_household)),
    ]


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


def test_us_a_baseline_and_its_reform_are_calculated_in_separate_requests() -> None:
    runtime = RecordingRuntime()
    baseline = calculate_household(
        _request("us", US_FAMILY),
        worker=_worker("us"),
        dependencies=HouseholdCalculationDependencies(),
    )

    reform = calculate_household(
        _request("us", US_FAMILY, policy={"parameters": CTC_REFORM}),
        worker=_worker("us"),
        dependencies=HouseholdCalculationDependencies(runtime=runtime),
    )

    assert isinstance(baseline, HouseholdExecutionResult), baseline
    assert isinstance(reform, HouseholdExecutionResult), reform
    assert reform.calculation_id == CALCULATION_ID
    assert reform.output.bundle == installed_household_bundle("us")
    baseline_situation = baseline.output.household.to_situation()
    reform_situation = reform.output.household.to_situation()
    # Same structure as the input, with each requested value filled in.
    assert list(baseline_situation) == list(US_FAMILY)
    assert baseline_situation["people"] == US_FAMILY["people"]
    baseline_ctc = baseline_situation["tax_units"]["your tax unit"]["ctc"]["2026"]
    reform_ctc = reform_situation["tax_units"]["your tax unit"]["ctc"]["2026"]
    assert isinstance(baseline_ctc, float)
    assert isinstance(reform_ctc, float)
    assert reform_ctc > baseline_ctc
    # A US household inherits the certified SPM selection and carries its receipt.
    assert reform.output.spm_config is not None
    assert reform.output.spm_config.geography_kind == "county"
    assert baseline.output.spm_provenance is not None
    assert reform.output.spm_provenance is not None
    assert reform.output.spm_provenance.scenario == reform.output.spm_config.scenario
    # One calculation span per request, for its one policy.
    assert [name for name, _ in runtime.spans] == [
        "v2_household_country_model_load",
        "v2_household_input_normalization",
        "v2_household_calculation",
    ]
    assert runtime.spans[2][1] == {"country": "us"}


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
        dependencies=HouseholdCalculationDependencies(),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    situation = outcome.output.household.to_situation()
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
        dependencies=HouseholdCalculationDependencies(),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    situation = outcome.output.household.to_situation()
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


US_SINGLE_WITHOUT_COUNTY = {
    "people": {"you": {"age": {"2026": 35}, "employment_income": {"2026": 30_000}}},
    "spm_units": {
        "your spm unit": {
            "members": ["you"],
            "spm_unit_spm_threshold": {"2026": None},
        }
    },
    "households": {
        "your household": {"members": ["you"], "state_name": {"2026": "TX"}}
    },
}


def test_us_a_chosen_spm_selection_must_have_its_geography() -> None:
    # The certified defaults measure by county. Inherited, a household without
    # a county leaves the SPM threshold uncalculated; chosen, it is an error.
    def calculate(**spm_settings):
        return calculate_household(
            _request("us", US_SINGLE_WITHOUT_COUNTY, **spm_settings),
            worker=_worker("us"),
            dependencies=HouseholdCalculationDependencies(),
        )

    inherited = calculate()
    chosen = calculate(spm={})
    national = calculate(spm={"geography_kind": "national"})

    assert isinstance(inherited, HouseholdExecutionResult), inherited
    unit = inherited.output.household.to_situation()["spm_units"]
    assert unit["your spm unit"]["spm_unit_spm_threshold"] == {"2026": None}
    assert isinstance(chosen, HouseholdExecutionFailure)
    assert chosen.error.code == "invalid_spm_selection"
    assert chosen.error.spm_error is not None
    assert chosen.error.spm_error.code == "SPM_GEOGRAPHY_REQUIRED"
    assert isinstance(national, HouseholdExecutionResult), national
    unit = national.output.household.to_situation()["spm_units"]
    assert isinstance(unit["your spm unit"]["spm_unit_spm_threshold"]["2026"], float)
    assert national.output.spm_config is not None
    assert national.output.spm_config.geography_kind == "national"
    receipt = national.output.spm_provenance
    assert receipt is not None
    assert receipt.geography_kind == "national"


# A household named by record IDs, as v2 households are: no person is "you".
US_RECORD_HOUSEHOLD_2025 = {
    "people": {
        "person-1": {"age": {"2025": 35}, "employment_income": {"2025": 30_000}},
        "person-2": {"age": {"2025": 5}},
    },
    "tax_units": {"tax-unit-1": {"members": ["person-1", "person-2"]}},
    "spm_units": {"spm-unit-1": {"members": ["person-1", "person-2"]}},
    "families": {"family-1": {"members": ["person-1", "person-2"]}},
    "marital_units": {
        "marital-unit-1": {"members": ["person-1"]},
        "marital-unit-2": {"members": ["person-2"]},
    },
    "households": {
        "household-1": {
            "members": ["person-1", "person-2"],
            "state_name": {"2025": "TX"},
            "county_fips": {"2025": "48201"},
        }
    },
}


def _periods(situation: dict) -> set[str]:
    """Return every period key of every variable in a situation."""
    return {
        str(period)
        for plural, entities in situation.items()
        if plural != "axes"
        for variables in entities.values()
        for values in variables.values()
        if isinstance(values, dict)
        for period in values
    }


def test_us_calculate_full_calculates_the_request_year_not_this_year() -> None:
    # The household is calculated in 2026 (or later), for its 2025 run.
    assert datetime.now(UTC).year >= 2026

    outcome = calculate_household(
        _request(
            "us", US_RECORD_HOUSEHOLD_2025, year="2025", add_missing_variables=True
        ),
        worker=_worker("us"),
        dependencies=HouseholdCalculationDependencies(),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    situation = outcome.output.household.to_situation()
    assert _periods(situation) == {"2025"}
    tax_unit = situation["tax_units"]["tax-unit-1"]
    household = situation["households"]["household-1"]
    assert isinstance(tax_unit["income_tax"]["2025"], float)
    assert isinstance(household["household_net_income"]["2025"], float)
    assert isinstance(situation["people"]["person-1"]["is_blind"]["2025"], bool)


def test_us_an_axis_without_a_period_varies_the_request_year() -> None:
    # The US model's own default year is 2024; an axis that named no period
    # used to vary 2024's income while the household was calculated for 2025.
    household = {
        **US_RECORD_HOUSEHOLD_2025,
        "people": {
            "person-1": {"age": {"2025": 35}, "employment_income": {"2025": 0}},
            "person-2": {"age": {"2025": 5}},
        },
        "households": {
            "household-1": {
                **US_RECORD_HOUSEHOLD_2025["households"]["household-1"],
                "household_net_income": {"2025": None},
            }
        },
        "axes": [[{"name": "employment_income", "count": 3, "min": 0, "max": 60_000}]],
    }

    outcome = calculate_household(
        _request("us", household, year="2025"),
        worker=_worker("us"),
        dependencies=HouseholdCalculationDependencies(),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    situation = outcome.output.household.to_situation()
    incomes = situation["households"]["household-1"]["household_net_income"]["2025"]
    assert incomes[0] < incomes[1] < incomes[2]
    assert situation["people"]["person-1"]["employment_income"] == {
        "2025": [0.0, 30_000.0, 60_000.0]
    }


UK_SINGLE = {
    "people": {"you": {"age": {"2026": 40}, "employment_income": {"2026": 30_000}}},
    "benunits": {"your benefit unit": {"members": ["you"]}},
    "households": {
        "your household": {"members": ["you"], "household_net_income": {"2026": None}}
    },
}


# The simulation service writes a stored policy value that has no end date as
# a range ending on 31 December 2200, the latest year a household job can be
# calculated for. These show how the household functions apply each form of
# period key in that year, with incomes high enough to be taxed at the
# changed rate after the thresholds' uprating.
SINGLE_ADULT_WITHOUT_PERIODS = {
    "us": {
        "people": {"you": {"age": 40, "employment_income": 100_000_000}},
        "tax_units": {"your tax unit": {"members": ["you"]}},
        "families": {"your family": {"members": ["you"]}},
        "spm_units": {"your spm unit": {"members": ["you"]}},
        "marital_units": {"your marital unit": {"members": ["you"]}},
        "households": {
            "your household": {
                "members": ["you"],
                "state_name": "TX",
                "household_net_income": None,
            }
        },
    },
    "uk": {
        "people": {"you": {"age": 40, "employment_income": 100_000_000}},
        "benunits": {"your benefit unit": {"members": ["you"]}},
        "households": {
            "your household": {"members": ["you"], "household_net_income": None}
        },
    },
}
# A parameter each country's reform lowers net income through, and its value.
CHANGED_PARAMETER = {
    "us": ("gov.irs.income.bracket.rates.7", 0.5),
    "uk": ("gov.hmrc.income_tax.rates.uk[0].rate", 0.3),
}


@pytest.mark.parametrize(
    ("country", "start_date_alone_applies"),
    [
        # policyengine-core reads a start date alone as "from that day on".
        ("us", True),
        # policyengine-uk's reform dictionary reads it as that one day.
        ("uk", False),
    ],
)
def test_reform_periods_apply_in_2200_only_through_a_stop_in_2200(
    country, start_date_alone_applies
) -> None:
    """A range applies through its stop date, in both countries.

    Only a range reaching the end of 2200 changes a 2200 calculation in both
    countries: a range ending in 2100 leaves it at current law, and a start
    date alone means a different period in each.
    """
    parameter, value = CHANGED_PARAMETER[country]
    reforms = {
        "through_2200": {parameter: {"2026-01-01.2200-12-31": value}},
        "through_2100": {parameter: {"2026-01-01.2100-12-31": value}},
        "start_date_alone": {parameter: {"2026-01-01": value}},
    }
    request = _request(
        country,
        SINGLE_ADULT_WITHOUT_PERIODS[country],
        year="2200",
        policies=[
            {"policy_key": "baseline", "parameters": {}},
            *(
                {"policy_key": policy_key, "parameters": parameters}
                for policy_key, parameters in reforms.items()
            ),
        ],
    )

    outcome = calculate_household(
        request,
        worker=_worker(country),
        dependencies=HouseholdCalculationDependencies(),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    net_incomes = {
        result.policy_key: result.household.to_situation()["households"][
            "your household"
        ]["household_net_income"]["2200"]
        for result in outcome.output.results
    }
    assert net_incomes["through_2200"] < net_incomes["baseline"]
    assert net_incomes["through_2100"] == net_incomes["baseline"]
    assert (net_incomes["start_date_alone"] < net_incomes["baseline"]) is (
        start_date_alone_applies
    )


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
        dependencies=HouseholdCalculationDependencies(),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    situation = outcome.output.household.to_situation()
    incomes = situation["households"]["your household"]["household_net_income"]
    assert len(incomes["2026"]) == 3
    assert incomes["2026"][0] < incomes["2026"][2]
    assert situation["people"]["you"]["employment_income"] == {
        "2026": [0.0, 30_000.0, 60_000.0]
    }
    assert outcome.output.spm_provenance is None
    assert outcome.output.spm_config is None


def test_uk_calculate_full_adds_every_missing_yearly_variable() -> None:
    outcome = calculate_household(
        _request("uk", UK_SINGLE, add_missing_variables=True),
        worker=_worker("uk"),
        dependencies=HouseholdCalculationDependencies(),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    you = outcome.output.household.to_situation()["people"]["you"]
    assert isinstance(you["income_tax"]["2026"], float)


def test_uk_unrecognized_inputs_fail_with_public_api_errors() -> None:
    household = {
        **UK_SINGLE,
        "people": {"you": {**UK_SINGLE["people"]["you"], "not_a_variable": 1}},
    }
    outcome = calculate_household(
        _request("uk", household),
        worker=_worker("uk"),
        dependencies=HouseholdCalculationDependencies(),
    )

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "invalid_inputs"
    assert outcome.error.message == (
        "Unrecognized calculate input(s): Unrecognized household variable "
        "`not_a_variable` at `household.people.you.not_a_variable`."
    )
    assert [error.path for error in outcome.error.input_errors] == [
        "household.people.you.not_a_variable"
    ]


def test_uk_unrecognized_policy_parameters_follow_the_households_inputs() -> None:
    # The household's unknown names are listed first, then the policy's, as
    # the Public API lists them for one request.
    household = {
        **UK_SINGLE,
        "people": {"you": {**UK_SINGLE["people"]["you"], "not_a_variable": 1}},
    }
    request = _request(
        "uk", household, policy={"parameters": {"gov.not.a.parameter": {}}}
    )

    outcome = calculate_household(
        request,
        worker=_worker("uk"),
        dependencies=HouseholdCalculationDependencies(),
    )

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "invalid_inputs"
    assert [error.path for error in outcome.error.input_errors] == [
        "household.people.you.not_a_variable",
        "policy.gov.not.a.parameter",
    ]


def test_uk_reform_values_given_as_text_are_converted_to_numbers() -> None:
    # The Public API converts a reform value to the parameter's type, reading
    # numeric text as a number first.
    basic_rate = "gov.hmrc.income_tax.rates.uk[0].rate"
    policies = (
        {"parameters": {}},
        {"parameters": {basic_rate: {"2026-01-01.2100-12-31": "0.3"}}},
    )

    outcomes = [
        calculate_household(
            _request("uk", UK_SINGLE, policy=policy),
            worker=_worker("uk"),
            dependencies=HouseholdCalculationDependencies(),
        )
        for policy in policies
    ]

    assert all(isinstance(outcome, HouseholdExecutionResult) for outcome in outcomes)
    baseline, reform = (
        outcome.output.household.to_situation()["households"]["your household"][
            "household_net_income"
        ]["2026"]
        for outcome in outcomes
        if isinstance(outcome, HouseholdExecutionResult)
    )
    assert isinstance(baseline, float)
    assert isinstance(reform, float)
    assert reform < baseline


def test_uk_deprecated_inputs_are_dropped_with_a_warning() -> None:
    household = {
        **UK_SINGLE,
        "people": {
            "you": {
                **UK_SINGLE["people"]["you"],
                "uc_has_limited_capability_for_work": {"2026": True},
            }
        },
    }
    outcome = calculate_household(
        _request("uk", household),
        worker=_worker("uk"),
        dependencies=HouseholdCalculationDependencies(),
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    (warning,) = outcome.output.warnings
    assert "uc_has_limited_capability_for_work" in warning
    you = outcome.output.household.to_situation()["people"]["you"]
    assert "uc_has_limited_capability_for_work" not in you


def test_uk_spm_settings_are_refused() -> None:
    outcome = calculate_household(
        _request("uk", UK_SINGLE, spm={}),
        worker=_worker("uk"),
        dependencies=HouseholdCalculationDependencies(),
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
        dependencies=HouseholdCalculationDependencies(),
    )

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "invalid_household"
    assert outcome.error.message.startswith("Invalid household payload: ")


def test_country_models_are_loaded_once_per_process() -> None:
    first = load_country_household_model("uk")

    assert load_country_household_model("uk") is first
    assert first.metadata.entities["benunit"].plural == "benunits"
    assert "gov.hmrc.income_tax.rates.uk[0].rate" in first.metadata.parameters
