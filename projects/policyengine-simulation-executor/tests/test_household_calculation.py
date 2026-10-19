"""Tests for v2 household calculations, ported from the Public API's behaviour."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from uuid import UUID

import numpy as np
from policyengine_simulation_contract.household_execution import (
    HouseholdBundleProvenance,
    HouseholdExecutionFailure,
    HouseholdExecutionRequest,
    HouseholdExecutionResult,
    HouseholdWorkerIdentity,
)
from policyengine_simulation_executor import household_calculation
from policyengine_simulation_executor.household_calculation import (
    CountryHouseholdModel,
    calculate_household,
    installed_household_bundle,
)
from policyengine_simulation_executor.household_inputs import (
    HouseholdEntity,
    HouseholdModelMetadata,
    HouseholdVariable,
)

CALCULATION_ID = UUID("00000000-0000-4000-8000-0000000000c1")
TODAY = date(2026, 6, 1)
WORKER = HouseholdWorkerIdentity(
    application_name="policyengine-simulation-v2-py6-7-5",
    function_name="calculate_household_us",
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


class RecordingSpans:
    def __init__(self) -> None:
        self.spans: list[tuple[str, dict]] = []

    @contextmanager
    def __call__(self, name: str, attributes: dict):
        self.spans.append((name, dict(attributes)))
        yield


# Fake country model: enough of a country package to exercise failure paths
# without importing a real one.


class FakePopulation:
    def __init__(self, ids: list[str], count: int) -> None:
        self.ids = ids
        self.count = count

    def get_index(self, entity_id: str) -> int:
        return self.ids.index(entity_id)


class FakeSystem:
    def __init__(self, value_types: dict[str, type]) -> None:
        self.value_types = value_types
        self.variables = dict.fromkeys(value_types)

    def get_variable(self, name: str):
        return SimpleNamespace(value_type=self.value_types[name])


def _fake_model(simulation_type) -> CountryHouseholdModel:
    value_types = {"age": int, "net_income": float, "spm_resources": float}
    metadata = HouseholdModelMetadata(
        variables={
            "age": HouseholdVariable(
                "age", "person", "year", is_input=True, default_value=40
            ),
            "net_income": HouseholdVariable(
                "net_income", "household", "year", is_input=False, default_value=0.0
            ),
            "spm_resources": HouseholdVariable(
                "spm_resources",
                "household",
                "year",
                is_input=False,
                default_value=0.0,
            ),
        },
        entities={
            "person": HouseholdEntity("person", "people", frozenset()),
            "household": HouseholdEntity(
                "household", "households", frozenset({"members"})
            ),
        },
        parameters=frozenset(),
    )
    system = FakeSystem(value_types)
    simulation_type.tax_benefit_system = system
    return CountryHouseholdModel(
        country="uk",
        package=SimpleNamespace(Simulation=simulation_type),
        tax_benefit_system=system,
        metadata=metadata,
    )


class CountryInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fake_simulation(outcomes: dict[str, object], *, axis_points: int = 1):
    class FakeSimulation:
        tax_benefit_system: FakeSystem

        def __init__(self, *, situation: dict, **_: object) -> None:
            self.situation = situation
            people = list(situation["people"])
            households = list(situation.get("households", {}))
            self.populations = {
                "people": FakePopulation(people, len(people) * axis_points),
                "households": FakePopulation(households, len(households) * axis_points),
            }

        def get_population(self, plural: str) -> FakePopulation:
            return self.populations[plural]

        def calculate(self, variable_name: str, period: str):
            outcome = outcomes[variable_name]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    return FakeSimulation


FAKE_HOUSEHOLD = {
    "people": {"you": {"age": {"2026": 30}}},
    "households": {
        "home": {
            "members": ["you"],
            "net_income": {"2026": None},
            "spm_resources": {"2026": None},
        }
    },
}


def _calculate_fake(simulation_type, household=None, *, country="uk", **overrides):
    model = _fake_model(simulation_type)
    return calculate_household(
        _request(country, household or FAKE_HOUSEHOLD, **overrides),
        worker=WORKER,
        model_loader=lambda country: model,
        today=TODAY,
    )


def test_a_value_that_cannot_be_calculated_is_null_without_a_warning() -> None:
    outcome = _calculate_fake(
        _fake_simulation(
            {"net_income": np.array([1.5]), "spm_resources": KeyError("missing")}
        )
    )

    assert isinstance(outcome, HouseholdExecutionResult)
    result = outcome.output.results[0]
    home = result.household.to_situation()["households"]["home"]
    assert home["net_income"] == {"2026": 1.5}
    assert home["spm_resources"] == {"2026": None}
    assert result.warnings == ()


def test_with_axes_a_failed_value_is_one_null_per_point_with_a_warning() -> None:
    household = {
        **FAKE_HOUSEHOLD,
        "axes": [[{"name": "age", "count": 3, "min": 0, "max": 2}]],
    }

    outcome = _calculate_fake(
        _fake_simulation(
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


def test_an_spm_input_error_fails_only_a_chosen_measurement() -> None:
    error = CountryInputError("SPM_GEOGRAPHY_REQUIRED", "SPM needs a county")
    outcomes = {"net_income": np.array([1.0]), "spm_resources": error}

    inherited = _calculate_fake(_fake_simulation(outcomes))
    chosen = _calculate_fake(_fake_simulation(outcomes), country="us", spm={})

    assert isinstance(inherited, HouseholdExecutionResult)
    assert inherited.output.results[0].household.to_situation()["households"]["home"][
        "spm_resources"
    ] == {"2026": None}
    assert isinstance(chosen, HouseholdExecutionFailure)
    assert chosen.error.code == "invalid_spm_selection"
    assert chosen.error.spm_error is not None
    assert chosen.error.spm_error.code == "SPM_GEOGRAPHY_REQUIRED"
    assert chosen.error.message == "SPM needs a county"


def test_simulation_build_failures_use_public_api_messages(monkeypatch) -> None:
    from policyengine_core.errors import SituationParsingError

    def parsing_failure(**_: object):
        raise SituationParsingError(["people"], "unknown person `ghost`")

    def runtime_failure(**_: object):
        msg = "model exploded"
        raise RuntimeError(msg)

    invalid = _calculate_fake(parsing_failure)
    failed = _calculate_fake(runtime_failure)

    assert isinstance(invalid, HouseholdExecutionFailure)
    assert invalid.error.code == "invalid_household"
    assert invalid.error.message.startswith("Invalid household payload: ")
    assert invalid.error.policy_key == "baseline"
    assert isinstance(failed, HouseholdExecutionFailure)
    assert failed.error.code == "calculation_failed"
    assert failed.error.message == (
        "Error calculating household under policy: model exploded"
    )
    assert failed.worker == WORKER


def test_a_request_for_another_bundle_is_refused() -> None:
    bundle = installed_household_bundle("uk").model_copy(
        update={"policyengine_version": "0.0.1"}
    )

    outcome = _calculate_fake(
        _fake_simulation({}), bundle=bundle.model_dump(mode="json")
    )

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "bundle_mismatch"


def test_installed_bundle_names_the_country_package() -> None:
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


def test_us_baseline_and_reform_run_in_one_request() -> None:
    spans = RecordingSpans()
    request = _request(
        "us",
        US_FAMILY,
        policies=[
            {"policy_key": "baseline", "parameters": {}},
            {"policy_key": "reform", "parameters": CTC_REFORM},
        ],
    )

    outcome = calculate_household(request, worker=WORKER, today=TODAY, span=spans)

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    assert outcome.calculation_id == CALCULATION_ID
    assert outcome.output.bundle == installed_household_bundle("us")
    baseline, reform = outcome.output.results
    assert (baseline.policy_key, reform.policy_key) == ("baseline", "reform")
    baseline_situation = baseline.household.to_situation()
    reform_situation = reform.household.to_situation()
    # Same structure as the input, with each requested value filled in.
    assert baseline_situation.keys() == US_FAMILY.keys()
    assert baseline_situation["people"] == US_FAMILY["people"]
    baseline_ctc = baseline_situation["tax_units"]["your tax unit"]["ctc"]["2026"]
    reform_ctc = reform_situation["tax_units"]["your tax unit"]["ctc"]["2026"]
    assert isinstance(baseline_ctc, float)
    assert reform_ctc > baseline_ctc
    # A US household inherits the certified SPM selection and carries its receipt.
    assert baseline.spm_provenance is not None
    assert baseline.spm_provenance.geography_kind == "county"
    assert [name for name, _ in spans.spans] == [
        "v2_household_country_model_load",
        "v2_household_input_normalization",
        "v2_household_calculation",
        "v2_household_calculation",
    ]
    assert spans.spans[3][1] == {"country": "us", "policy_key": "reform"}


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

    outcome = calculate_household(_request("uk", household), worker=WORKER, today=TODAY)

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    situation = outcome.output.results[0].household.to_situation()
    incomes = situation["households"]["your household"]["household_net_income"]
    assert len(incomes["2026"]) == 3
    assert incomes["2026"][0] < incomes["2026"][2]
    assert situation["people"]["you"]["employment_income"] == {
        "2026": [0.0, 30_000.0, 60_000.0]
    }
    assert outcome.output.results[0].spm_provenance is None


def test_calculate_full_adds_every_missing_yearly_variable() -> None:
    outcome = calculate_household(
        _request("uk", UK_SINGLE, add_missing_variables=True),
        worker=WORKER,
        today=TODAY,
    )

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    you = outcome.output.results[0].household.to_situation()["people"]["you"]
    assert "income_tax" in you
    assert isinstance(you["income_tax"]["2026"], float)


def test_unrecognized_inputs_fail_with_public_api_errors() -> None:
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

    outcome = calculate_household(request, worker=WORKER, today=TODAY)

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


def test_deprecated_inputs_warn_in_every_policy_result() -> None:
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

    outcome = calculate_household(request, worker=WORKER, today=TODAY)

    assert isinstance(outcome, HouseholdExecutionResult), outcome
    for result in outcome.output.results:
        assert len(result.warnings) == 1
        assert "uc_has_limited_capability_for_work" in result.warnings[0]
        assert (
            "uc_has_limited_capability_for_work"
            not in (result.household.to_situation()["people"]["you"])
        )


def test_spm_settings_for_the_uk_are_refused() -> None:
    outcome = calculate_household(
        _request("uk", UK_SINGLE, spm={}), worker=WORKER, today=TODAY
    )

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "invalid_spm_selection"
    assert outcome.error.spm_error is not None
    assert outcome.error.spm_error.code == "SPM_SETTINGS_UNSUPPORTED"


def test_a_member_who_is_not_a_person_is_an_invalid_household() -> None:
    household = {
        **UK_SINGLE,
        "benunits": {"your benefit unit": {"members": ["you", "ghost"]}},
    }

    outcome = calculate_household(_request("uk", household), worker=WORKER, today=TODAY)

    assert isinstance(outcome, HouseholdExecutionFailure)
    assert outcome.error.code == "invalid_household"
    assert outcome.error.message.startswith("Invalid household payload: ")


def test_country_models_are_loaded_once_per_process() -> None:
    first = household_calculation.load_country_household_model("uk")

    assert household_calculation.load_country_household_model("uk") is first
    assert first.metadata.entities["benunit"].plural == "benunits"
    assert "gov.hmrc.income_tax.rates.uk[0].rate" in first.metadata.parameters
