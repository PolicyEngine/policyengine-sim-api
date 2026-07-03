"""Contract tests for simulation API response shapes."""

from src.modal.gateway.generate_openapi import create_openapi_app
from src.modal.gateway.models import (
    BudgetWindowAnnualImpact,
    BudgetWindowResult,
    BudgetWindowTotals,
    JobStatusResponse,
)
from policyengine_simulation_executor.simulation_macro_output import SingleYearMacroOutput

from fixtures.test_simulation_api_contracts import (
    CURRENT_REQUIRED_BUDGET_KEYS,
    CURRENT_SINGLE_YEAR_MACRO_KEYS,
    CURRENT_SINGLE_YEAR_MACRO_RESULT,
)


def test_job_status_result_preserves_current_single_year_macro_dict_contract():
    response = JobStatusResponse(
        status="complete",
        result=CURRENT_SINGLE_YEAR_MACRO_RESULT,
    )

    assert response.result is not None
    assert set(response.result) == CURRENT_SINGLE_YEAR_MACRO_KEYS
    assert set(response.result["budget"]) == CURRENT_REQUIRED_BUDGET_KEYS
    assert (
        response.model_dump(mode="json")["result"] == CURRENT_SINGLE_YEAR_MACRO_RESULT
    )


def test_internal_single_year_macro_schema_matches_current_public_keys():
    assert set(SingleYearMacroOutput.model_fields) == CURRENT_SINGLE_YEAR_MACRO_KEYS


def test_internal_single_year_macro_schema_serializes_current_public_contract():
    output = SingleYearMacroOutput.model_validate(CURRENT_SINGLE_YEAR_MACRO_RESULT)

    assert output.model_dump(mode="json") == CURRENT_SINGLE_YEAR_MACRO_RESULT


def test_openapi_keeps_job_status_result_as_unstructured_dict():
    spec = create_openapi_app().openapi()
    schemas = spec["components"]["schemas"]

    assert "SingleYearMacroOutput" not in schemas
    result_schema = schemas["JobStatusResponse"]["properties"]["result"]
    assert result_schema == {
        "anyOf": [
            {
                "additionalProperties": True,
                "type": "object",
            },
            {
                "type": "null",
            },
        ],
        "title": "Result",
    }


def test_budget_window_result_keeps_compact_public_contract():
    result = BudgetWindowResult(
        startYear="2026",
        endYear="2027",
        windowSize=2,
        annualImpacts=[
            BudgetWindowAnnualImpact(
                year="2026",
                taxRevenueImpact=10.0,
                federalTaxRevenueImpact=7.0,
                stateTaxRevenueImpact=3.0,
                benefitSpendingImpact=2.0,
                budgetaryImpact=8.0,
            )
        ],
        totals=BudgetWindowTotals(
            taxRevenueImpact=10.0,
            federalTaxRevenueImpact=7.0,
            stateTaxRevenueImpact=3.0,
            benefitSpendingImpact=2.0,
            budgetaryImpact=8.0,
        ),
    )

    dumped = result.model_dump(mode="json")
    assert set(dumped) == {
        "kind",
        "startYear",
        "endYear",
        "windowSize",
        "annualImpacts",
        "totals",
    }
    assert dumped["kind"] == "budgetWindow"
    assert dumped["totals"]["year"] == "Total"
    assert "outputsByYear" not in dumped


def test_openapi_keeps_budget_window_result_compact():
    spec = create_openapi_app().openapi()
    budget_window_schema = spec["components"]["schemas"]["BudgetWindowResult"]

    assert set(budget_window_schema["properties"]) == {
        "kind",
        "startYear",
        "endYear",
        "windowSize",
        "annualImpacts",
        "totals",
    }
    assert "outputsByYear" not in budget_window_schema["properties"]
