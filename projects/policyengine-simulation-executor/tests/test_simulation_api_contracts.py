"""Contract tests for simulation API response shapes."""

from fixtures.macro_output import (
    CURRENT_REQUIRED_BUDGET_KEYS,
    CURRENT_SINGLE_YEAR_MACRO_KEYS,
    CURRENT_SINGLE_YEAR_MACRO_RESULT,
)
from policyengine_simulation_contract.gateway_models import (
    BudgetWindowAnnualImpact,
    BudgetWindowResult,
    BudgetWindowTotals,
    JobStatusResponse,
)
from policyengine_simulation_executor.simulation_macro_output import (
    SingleYearMacroOutput,
)
from policyengine_simulation_gateway.generate_openapi import create_openapi_app

# The optional per-state blocks of a US national run (issue #829). The gateway
# drops the field when it is None (response_model_exclude_none).
NATIONAL_RESULT_KEYS = CURRENT_SINGLE_YEAR_MACRO_KEYS | {"regions"}


def test_job_status_result_preserves_current_single_year_macro_dict_contract():
    response = JobStatusResponse(
        status="complete",
        result=CURRENT_SINGLE_YEAR_MACRO_RESULT,
    )

    assert response.result is not None
    dumped_result = response.result.model_dump(mode="json")
    assert set(dumped_result) == NATIONAL_RESULT_KEYS
    assert set(dumped_result["budget"]) == CURRENT_REQUIRED_BUDGET_KEYS
    assert response.model_dump(mode="json")["result"] == {
        **CURRENT_SINGLE_YEAR_MACRO_RESULT,
        "regions": None,
    }


def test_job_status_result_round_trips_per_state_blocks():
    block = {
        key: value
        for key, value in CURRENT_SINGLE_YEAR_MACRO_RESULT.items()
        if key != "spm_provenance"
    }
    result = {**CURRENT_SINGLE_YEAR_MACRO_RESULT, "regions": {"state/ca": block}}
    response = JobStatusResponse.model_validate(
        {"status": "complete", "result": result}
    )

    dumped = response.model_dump(mode="json", exclude_none=True)["result"]
    assert set(dumped["regions"]) == {"state/ca"}
    assert (
        dumped["regions"]["state/ca"]
        == JobStatusResponse.model_validate(
            {"status": "complete", "result": block}
        ).model_dump(mode="json", exclude_none=True)["result"]
    )


def test_internal_single_year_macro_schema_matches_current_public_keys():
    assert set(SingleYearMacroOutput.model_fields) == NATIONAL_RESULT_KEYS


def test_internal_single_year_macro_schema_serializes_current_public_contract():
    output = SingleYearMacroOutput.model_validate(CURRENT_SINGLE_YEAR_MACRO_RESULT)

    assert output.model_dump(mode="json", exclude={"regions"}) == (
        CURRENT_SINGLE_YEAR_MACRO_RESULT
    )


def test_openapi_publishes_the_single_year_macro_result_schema():
    spec = create_openapi_app().openapi()
    schemas = spec["components"]["schemas"]

    assert "SingleYearMacroOutput" in schemas
    assert set(schemas["SingleYearMacroOutput"]["properties"]) == (NATIONAL_RESULT_KEYS)
    assert set(schemas["RegionMacroOutput"]["properties"]) == (
        CURRENT_SINGLE_YEAR_MACRO_KEYS
    )
    result_schema = schemas["JobStatusResponse"]["properties"]["result"]
    assert result_schema == {
        "anyOf": [
            {
                "$ref": "#/components/schemas/SingleYearMacroOutput",
            },
            {
                "type": "null",
            },
        ]
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
