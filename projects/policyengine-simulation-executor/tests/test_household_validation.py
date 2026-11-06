"""Tests for the household that checks a v2 household function calculates."""

from __future__ import annotations

from copy import deepcopy

import pytest
from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionRequest,
    HouseholdWorkerIdentity,
)
from policyengine_simulation_executor.household_calculation import (
    calculate_household,
    installed_household_bundle,
)
from policyengine_simulation_executor.household_validation import (
    check_household_validation,
    household_validation_request,
)

WORKER = HouseholdWorkerIdentity(
    application_name="policyengine-simulation-v2-py-test",
    function_name="calculate_household_uk",
)


def _succeeded(request: HouseholdExecutionRequest) -> dict:
    situation = request.household.to_situation()
    situation["households"]["your household"]["household_net_income"] = {
        "2026": 25_000.0
    }
    return {
        "status": "succeeded",
        "schema_version": 1,
        "calculation_id": str(request.calculation_id),
        "output": {
            "country": request.country,
            "bundle": request.bundle.model_dump(mode="json"),
            "results": [{"policy_key": "baseline", "household": situation}],
        },
        "worker": WORKER.model_dump(mode="json"),
    }


@pytest.mark.parametrize(
    ("country", "group_plural"), [("us", "spm_units"), ("uk", "benunits")]
)
def test_the_validation_household_is_one_adult_under_current_law(
    country, group_plural
) -> None:
    request = household_validation_request(country, year=2026)

    situation = request.household.to_situation()
    assert request.country == country
    assert request.year == "2026"
    assert request.bundle == installed_household_bundle(country)
    assert [policy.parameters for policy in request.policies] == [{}]
    assert list(situation["people"]) == ["you"]
    assert situation[group_plural] == {"your unit": {"members": ["you"]}}
    assert situation["households"]["your household"]["household_net_income"] == {
        "2026": None
    }


def test_a_calculated_net_income_passes_the_check() -> None:
    request = household_validation_request("uk", year=2026)

    net_income = check_household_validation(
        _succeeded(request), request=request, worker=WORKER
    )

    assert net_income == 25_000.0


def _failed(outcome: dict) -> dict:
    return {
        "status": "failed",
        "schema_version": 1,
        "calculation_id": outcome["calculation_id"],
        "error": {"code": "calculation_failed", "message": "model exploded"},
        "worker": outcome["worker"],
    }


def _with_another_bundle(outcome: dict) -> dict:
    changed = deepcopy(outcome)
    changed["output"]["bundle"]["policyengine_version"] = "0.0.1"
    return changed


def _from_another_function(outcome: dict) -> dict:
    changed = deepcopy(outcome)
    changed["worker"]["function_name"] = "calculate_household_us"
    return changed


def _without_net_income(outcome: dict) -> dict:
    changed = deepcopy(outcome)
    household = changed["output"]["results"][0]["household"]
    household["households"]["your household"]["household_net_income"] = {"2026": None}
    return changed


@pytest.mark.parametrize(
    ("change_outcome", "message"),
    [
        (_failed, "did not calculate the validation household"),
        (_with_another_bundle, "calculated with another bundle"),
        (_from_another_function, "reported another function"),
        (_without_net_income, "returned no household net income"),
    ],
)
def test_an_outcome_that_is_not_the_calculated_household_fails_the_check(
    change_outcome, message
) -> None:
    request = household_validation_request("uk", year=2026)

    with pytest.raises(RuntimeError, match=f"calculate_household_uk {message}"):
        check_household_validation(
            change_outcome(_succeeded(request)), request=request, worker=WORKER
        )


@pytest.mark.parametrize("country", ["us", "uk"])
def test_the_installed_models_calculate_the_validation_household(country) -> None:
    # Release validation and the image check depend on this household, so the
    # PR checks calculate it with the installed country models too.
    request = household_validation_request(country, year=2026)
    worker = WORKER.model_copy(
        update={"function_name": f"calculate_household_{country}"}
    )

    outcome = calculate_household(request, worker=worker, required_country=country)

    net_income = check_household_validation(
        outcome.model_dump(mode="json"), request=request, worker=worker
    )
    assert net_income > 0
