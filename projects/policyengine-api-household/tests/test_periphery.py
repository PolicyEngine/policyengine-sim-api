"""Tests for the validation/warnings periphery ported from the v1
household API: deprecated-input filtering, period-key validation,
partial-month warnings, budget validation, axes caps, and the
warnings response envelope.

Unit tests use a stub tax-benefit system so they stay fast; endpoint
tests share the (lazily loaded, process-cached) policyengine-us model
with tests/test_calculate.py.
"""

import pytest
from fastapi.testclient import TestClient

from policyengine_api_household.main import app
from policyengine_api_household.periods import (
    detect_period_warnings,
    normalize_period_keys,
    validate_period_budgets,
    validate_period_keys,
)
from policyengine_api_household.utils.deprecated_inputs import (
    drop_deprecated_inputs,
)


# ---------------------------------------------------------------------------
# Stub system for unit tests
# ---------------------------------------------------------------------------


class _StubVariable:
    def __init__(self, definition_period="year", value_type=float):
        self.definition_period = definition_period
        self.value_type = value_type


class _StubSystem:
    def __init__(self, variables):
        self.variables = variables


STUB_SYSTEM = _StubSystem(
    {
        "employment_income": _StubVariable("year", float),
        "care_cost": _StubVariable("month", float),
        "is_disabled": _StubVariable("month", bool),
        "snap": _StubVariable("month", float),
    }
)


# ---------------------------------------------------------------------------
# Deprecated-input filtering (unit)
# ---------------------------------------------------------------------------


def test_drop_deprecated_inputs_strips_and_warns():
    household = {
        "people": {
            "adult": {
                "age": {"2026": 40},
                "medical_out_of_pocket_expenses": {"2026": 1200},
            }
        }
    }
    result = drop_deprecated_inputs(household)

    # Original is not mutated.
    assert "medical_out_of_pocket_expenses" in household["people"]["adult"]
    # Copy is cleaned.
    assert "medical_out_of_pocket_expenses" not in result.household["people"]["adult"]
    assert result.household["people"]["adult"]["age"] == {"2026": 40}

    assert len(result.warnings) == 1
    assert result.warnings[0].message == (
        "Input `medical_out_of_pocket_expenses` on `people/adult` is "
        "deprecated and was ignored for this calculation. Removed in "
        "policyengine-us 1.673.0. Migrate non-premium spending to "
        "`other_medical_expenses` and premium spending to "
        "`health_insurance_premiums`."
    )


def test_drop_deprecated_inputs_strips_axes_entries():
    household = {
        "people": {"adult": {"age": {"2026": 40}}},
        "axes": [
            [
                {"name": "medical_out_of_pocket_expenses", "count": 5},
                {"name": "employment_income", "count": 5},
            ]
        ],
    }
    result = drop_deprecated_inputs(household)
    assert result.household["axes"] == [[{"name": "employment_income", "count": 5}]]
    assert len(result.warnings) == 1
    assert "`axes[0][0].name`" in result.warnings[0].message


def test_drop_deprecated_inputs_removes_axes_key_when_all_dropped():
    household = {
        "people": {"adult": {"age": {"2026": 40}}},
        "axes": [{"name": "medical_out_of_pocket_expenses", "count": 5}],
    }
    result = drop_deprecated_inputs(household)
    assert "axes" not in result.household
    assert len(result.warnings) == 1


def test_drop_deprecated_inputs_noop_without_deprecated_keys():
    household = {"people": {"adult": {"age": {"2026": 40}}}}
    result = drop_deprecated_inputs(household)
    assert result.household == household
    assert result.warnings == []


# ---------------------------------------------------------------------------
# Period-key validation (unit)
# ---------------------------------------------------------------------------


def test_validate_period_keys_accepts_year_and_month():
    household = {
        "people": {
            "adult": {"employment_income": {"2026": 1000, "2026-01": 10}}
        }
    }
    validate_period_keys(household, STUB_SYSTEM)  # should not raise


def test_validate_period_keys_rejects_malformed_month():
    household = {"people": {"adult": {"employment_income": {"2026-15": 10}}}}
    with pytest.raises(ValueError) as excinfo:
        validate_period_keys(household, STUB_SYSTEM)
    assert str(excinfo.value) == (
        "Invalid period key `2026-15` for `employment_income` on "
        '`people/adult`. Expected a year (e.g. "2026") or a month '
        '(e.g. "2026-01").'
    )


# ---------------------------------------------------------------------------
# Budget validation (unit)
# ---------------------------------------------------------------------------


def _twelve_months(year: str, value: float) -> dict:
    return {f"{year}-{m:02d}": value for m in range(1, 13)}


def test_validate_period_budgets_rejects_full_year_mismatch():
    period_map = {"2026": 1200.0, **_twelve_months("2026", 200.0)}
    household = {"people": {"adult": {"care_cost": period_map}}}
    with pytest.raises(ValueError) as excinfo:
        validate_period_budgets(household, STUB_SYSTEM)
    assert str(excinfo.value) == (
        "Inconsistent input: monthly values for `care_cost` on "
        "`people/adult` in 2026 sum to 2400.0, which doesn't match the "
        "annual total 1200.0."
    )


def test_validate_period_budgets_allows_partial_months():
    # Partial monthly overrides are silently distributed, even when the
    # remainder is negative — matching v1 / OpenFisca.
    household = {
        "people": {"adult": {"care_cost": {"2026": 100.0, "2026-01": 500.0}}}
    }
    validate_period_budgets(household, STUB_SYSTEM)  # should not raise


def test_validate_period_budgets_allows_matching_full_year():
    period_map = {"2026": 2400.0, **_twelve_months("2026", 200.0)}
    household = {"people": {"adult": {"care_cost": period_map}}}
    validate_period_budgets(household, STUB_SYSTEM)  # should not raise


# ---------------------------------------------------------------------------
# Partial-month warnings (unit)
# ---------------------------------------------------------------------------


def test_detect_period_warnings_partial_month_with_annual_output():
    household = {
        "people": {"adult": {"care_cost": {"2026-01": 100.0}}},
        "spm_units": {"spm": {"snap": {"2026": None}}},
    }
    warnings = detect_period_warnings(household, STUB_SYSTEM)
    assert len(warnings) == 1
    assert warnings[0].message == (
        "`care_cost` on `people/adult` was keyed for 1 of 12 months in "
        "2026 (2026-01); the remaining 11 months will read the engine's "
        "fallback value (often 0, sometimes a formula-derived value), "
        "not the value you set. Because an annual output is requested "
        "for 2026, those fallback values are summed into the annual "
        "total and may not match what you intended. To get an accurate "
        'annual figure, either send a yearly key (`{"2026": V}`) '
        "or set all 12 monthly keys."
    )


def test_detect_period_warnings_silent_without_annual_output():
    household = {"people": {"adult": {"care_cost": {"2026-01": 100.0}}}}
    assert detect_period_warnings(household, STUB_SYSTEM) == []


def test_detect_period_warnings_silent_for_year_defined_variables():
    # employment_income is YEAR-defined in the stub (as in policyengine-us),
    # so a monthly key on it never warns.
    household = {
        "people": {"adult": {"employment_income": {"2026-01": 2500}}},
        "spm_units": {"spm": {"snap": {"2026": None}}},
    }
    assert detect_period_warnings(household, STUB_SYSTEM) == []


def test_detect_period_warnings_silent_when_year_input_present():
    # A non-null year input means the normalizer fills the unset months
    # from the remainder — no fallback hazard, no warning.
    household = {
        "people": {
            "adult": {"care_cost": {"2026": 1200.0, "2026-01": 100.0}}
        },
        "spm_units": {"spm": {"snap": {"2026": None}}},
    }
    assert detect_period_warnings(household, STUB_SYSTEM) == []


def test_detect_period_warnings_silent_with_all_12_months():
    household = {
        "people": {"adult": {"care_cost": _twelve_months("2026", 100.0)}},
        "spm_units": {"spm": {"snap": {"2026": None}}},
    }
    assert detect_period_warnings(household, STUB_SYSTEM) == []


# ---------------------------------------------------------------------------
# Period normalization (unit)
# ---------------------------------------------------------------------------


def test_normalize_distributes_numeric_year_over_unset_months():
    household = {
        "people": {"adult": {"care_cost": {"2026": 1200.0, "2026-01": 100.0}}}
    }
    normalized = normalize_period_keys(household, STUB_SYSTEM)
    period_map = normalized["people"]["adult"]["care_cost"]
    assert "2026" not in period_map
    assert period_map["2026-01"] == 100.0
    per_unset = (1200.0 - 100.0) / 11
    for m in range(2, 13):
        assert period_map[f"2026-{m:02d}"] == per_unset
    # Original untouched.
    assert household["people"]["adult"]["care_cost"]["2026"] == 1200.0


def test_normalize_broadcasts_non_numeric_year_value():
    household = {
        "people": {"adult": {"is_disabled": {"2026": True, "2026-03": False}}}
    }
    normalized = normalize_period_keys(household, STUB_SYSTEM)
    period_map = normalized["people"]["adult"]["is_disabled"]
    assert "2026" not in period_map
    assert period_map["2026-03"] is False
    assert all(period_map[f"2026-{m:02d}"] is True for m in (1, 2, 4, 12))


def test_normalize_keeps_null_year_output_requests():
    household = {"spm_units": {"spm": {"snap": {"2026": None}}}}
    normalized = normalize_period_keys(household, STUB_SYSTEM)
    assert normalized["spm_units"]["spm"]["snap"] == {"2026": None}


def test_normalize_leaves_year_defined_variables_alone():
    household = {"people": {"adult": {"employment_income": {"2026": 30000}}}}
    normalized = normalize_period_keys(household, STUB_SYSTEM)
    assert normalized == household


# ---------------------------------------------------------------------------
# Endpoint tests (real policyengine-us model — slow on first calculate)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _us_household(**people_extra) -> dict:
    people = {"adult": {"age": {"2026": 35}, **people_extra}}
    return {
        "people": people,
        "tax_units": {"tu": {"members": ["adult"], "income_tax": {"2026": None}}},
        "spm_units": {"spm": {"members": ["adult"]}},
        "families": {"fam": {"members": ["adult"]}},
        "marital_units": {"mu": {"members": ["adult"]}},
        "households": {
            "hh": {"members": ["adult"], "state_name": {"2026": "TX"}}
        },
    }


def test_calculate_deprecated_input_warns_and_computes(client):
    household = _us_household(
        employment_income={"2026": 50000},
        medical_out_of_pocket_expenses={"2026": 1200},
    )
    response = client.post("/us/calculate", json={"household": household})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["warnings"] == [
        "Input `medical_out_of_pocket_expenses` on `people/adult` is "
        "deprecated and was ignored for this calculation. Removed in "
        "policyengine-us 1.673.0. Migrate non-premium spending to "
        "`other_medical_expenses` and premium spending to "
        "`health_insurance_premiums`."
    ]
    # The deprecated key is stripped from the echoed result.
    assert "medical_out_of_pocket_expenses" not in body["result"]["people"]["adult"]
    # The rest of the calculation still ran.
    income_tax = body["result"]["tax_units"]["tu"]["income_tax"]["2026"]
    assert isinstance(income_tax, (int, float))


def test_calculate_no_warnings_key_on_clean_request(client):
    household = _us_household(employment_income={"2026": 30000})
    response = client.post("/us/calculate", json={"household": household})
    assert response.status_code == 200, response.text
    assert "warnings" not in response.json()


def test_calculate_rejects_malformed_period_key(client):
    household = _us_household(employment_income={"2026-15": 2500})
    response = client.post("/us/calculate", json={"household": household})
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "error"
    assert body["message"] == (
        "Invalid period key `2026-15` for `employment_income` on "
        '`people/adult`. Expected a year (e.g. "2026") or a month '
        '(e.g. "2026-01").'
    )


def test_calculate_rejects_inconsistent_annual_budget(client):
    monthly = {f"2026-{m:02d}": 200.0 for m in range(1, 13)}
    household = _us_household(
        pre_subsidy_care_expenses={"2026": 1200.0, **monthly}
    )
    response = client.post("/us/calculate", json={"household": household})
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "error"
    assert body["message"].startswith(
        "Inconsistent input: monthly values for `pre_subsidy_care_expenses` "
        "on `people/adult` in 2026 sum to 2400.0"
    )


def test_calculate_partial_month_warning(client):
    household = _us_household(pre_subsidy_care_expenses={"2026-01": 100.0})
    household["spm_units"]["spm"]["snap"] = {"2026": None}
    response = client.post("/us/calculate", json={"household": household})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert any(
        w.startswith(
            "`pre_subsidy_care_expenses` on `people/adult` was keyed for "
            "1 of 12 months in 2026 (2026-01);"
        )
        for w in body.get("warnings", [])
    ), body.get("warnings")


def test_calculate_axes_sweep_returns_expanded_lists(client):
    household = _us_household(employment_income={"2026": None})
    household["axes"] = [
        [
            {
                "name": "employment_income",
                "count": 5,
                "min": 0,
                "max": 100000,
                "period": "2026",
            }
        ]
    ]
    response = client.post("/us/calculate", json={"household": household})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert "warnings" not in body
    incomes = body["result"]["people"]["adult"]["employment_income"]["2026"]
    assert incomes == [0.0, 25000.0, 50000.0, 75000.0, 100000.0]
    income_tax = body["result"]["tax_units"]["tu"]["income_tax"]["2026"]
    assert isinstance(income_tax, list) and len(income_tax) == 5
    # Axes echo back in the result, as in v1.
    assert body["result"]["axes"] == household["axes"]


@pytest.mark.parametrize("count,expected_got", [(0, 0), (101, 101), ("7.5", None)])
def test_calculate_axes_count_cap(client, count, expected_got):
    household = _us_household(employment_income={"2026": None})
    household["axes"] = [
        [
            {
                "name": "employment_income",
                "count": count,
                "min": 0,
                "max": 100000,
                "period": "2026",
            }
        ]
    ]
    response = client.post("/us/calculate", json={"household": household})
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "error"
    if expected_got is None:
        assert body["message"] == "'axes[0].count' must be an integer"
    else:
        assert body["message"] == (
            f"'axes[0].count' must be between 1 and 100; got {expected_got}"
        )


def test_calculate_axes_entry_cap(client):
    household = _us_household(employment_income={"2026": None})
    household["axes"] = [
        {"name": "employment_income", "count": 2, "min": 0, "max": 1000, "period": "2026"}
    ] * 11
    response = client.post("/us/calculate", json={"household": household})
    assert response.status_code == 400
    body = response.json()
    assert body["message"] == "'axes' may contain at most 10 entries; got 11"


def test_calculate_axes_requires_name(client):
    household = _us_household(employment_income={"2026": None})
    household["axes"] = [{"count": 2, "min": 0, "max": 1000, "period": "2026"}]
    response = client.post("/us/calculate", json={"household": household})
    assert response.status_code == 400
    assert response.json()["message"] == (
        "'axes[0].name' must be a non-empty string"
    )


def test_calculate_axes_must_be_list(client):
    household = _us_household(employment_income={"2026": None})
    household["axes"] = {"name": "employment_income"}
    response = client.post("/us/calculate", json={"household": household})
    assert response.status_code == 400
    assert response.json()["message"] == "'axes' must be a list"
