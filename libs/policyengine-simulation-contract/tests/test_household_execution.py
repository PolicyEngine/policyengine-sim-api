"""Tests for the versioned household calculation contracts."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from policyengine_simulation_contract.household_execution import (
    MAXIMUM_AXIS_POINTS,
    MAXIMUM_POLICIES_PER_REQUEST,
    CalculatedHousehold,
    FailedHouseholdCalculation,
    HouseholdBundleProvenance,
    HouseholdCalculationError,
    HouseholdCalculationOutput,
    HouseholdDocument,
    HouseholdExecutionFailure,
    HouseholdExecutionRequest,
    HouseholdExecutionResult,
    HouseholdInputError,
    HouseholdPolicy,
    HouseholdPolicyResult,
    HouseholdWorkerIdentity,
    PendingHouseholdCalculation,
    RunningHouseholdCalculation,
    SubmitHouseholdCalculationRequest,
    SucceededHouseholdCalculation,
    parse_household_calculation_poll_response,
    parse_household_execution_outcome,
)
from policyengine_simulation_contract.spm import SPMErrorDetail
from pydantic import ValidationError

CALCULATION_ID = UUID("00000000-0000-4000-8000-0000000000c1")
JOB_ID = UUID("00000000-0000-4000-8000-0000000000d2")

# A household in the situation shape the Public API's calculate routes accept.
US_SITUATION = {
    "people": {
        "you": {"age": {"2026": 40}, "employment_income": {"2026": 50_000}},
        "your partner": {"age": {"2026": 38}},
    },
    "households": {
        "your household": {
            "members": ["you", "your partner"],
            "state_name": {"2026": "CA"},
            "household_net_income": {"2026": None},
        }
    },
    "tax_units": {"your tax unit": {"members": ["you", "your partner"]}},
    "axes": [
        [
            {
                "name": "employment_income",
                "count": 11,
                "min": 0,
                "max": 100_000,
                "period": "2026",
            }
        ]
    ],
}


def _bundle() -> HouseholdBundleProvenance:
    return HouseholdBundleProvenance(
        policyengine_version="6.7.5",
        core_package_version="3.33.0",
        country_package_name="policyengine-us",
        country_package_version="2.2.1",
        bundle_manifest_sha256="a" * 64,
    )


def _policies() -> tuple[HouseholdPolicy, ...]:
    return (
        HouseholdPolicy(policy_key="baseline", parameters={}),
        HouseholdPolicy(
            policy_key="reform",
            parameters={
                "gov.irs.credits.ctc.amount.base[0].amount": {
                    "2026-01-01.2100-12-31": 3_000
                }
            },
        ),
    )


def _request(**overrides: object) -> HouseholdExecutionRequest:
    values: dict[str, object] = {
        "calculation_id": CALCULATION_ID,
        "country": "us",
        "household": US_SITUATION,
        "policies": _policies(),
        "bundle": _bundle(),
    }
    values.update(overrides)
    return HouseholdExecutionRequest.model_validate(values)


def _worker() -> HouseholdWorkerIdentity:
    return HouseholdWorkerIdentity(
        application_name="policyengine-simulation-v2-py6-7-5",
        function_name="calculate_household_us",
    )


def test_household_document_keeps_the_public_api_situation_shape() -> None:
    document = HouseholdDocument.model_validate(US_SITUATION)

    assert document.to_situation() == US_SITUATION
    assert document.group_collections() == {
        "households": document.to_situation()["households"],
        "tax_units": document.to_situation()["tax_units"],
    }
    assert document.has_axes


GROUPS_BEFORE_PEOPLE = {
    "households": US_SITUATION["households"],
    "people": US_SITUATION["people"],
    "tax_units": US_SITUATION["tax_units"],
    "axes": US_SITUATION["axes"],
}


@pytest.mark.parametrize("situation", [US_SITUATION, GROUPS_BEFORE_PEOPLE])
def test_households_serialize_their_keys_in_the_callers_order(situation) -> None:
    # The Public API walks a household in the order its caller sent it, so
    # unrecognized inputs and warnings are listed in that order.
    document = HouseholdDocument.model_validate(situation)
    request = _request(household=situation)
    transported = HouseholdExecutionRequest.model_validate(
        json.loads(request.model_dump_json())
    )
    calculated = CalculatedHousehold.model_validate(situation)

    assert list(document.to_situation()) == list(situation)
    assert list(json.loads(document.model_dump_json())) == list(situation)
    assert list(transported.household.to_situation()) == list(situation)
    assert list(calculated.to_situation()) == list(situation)


def test_household_without_axes_never_gains_an_axes_key() -> None:
    situation = {key: value for key, value in US_SITUATION.items() if key != "axes"}

    document = HouseholdDocument.model_validate(situation)

    assert not document.has_axes
    assert "axes" not in document.to_situation()
    assert "axes" not in json.loads(document.model_dump_json())


def test_household_document_accepts_values_without_periods() -> None:
    situation = {
        "people": {"you": {"age": 40, "is_blind": False}},
        "households": {"your household": {"members": ["you"]}},
    }

    assert HouseholdDocument.model_validate(situation).to_situation() == situation


@pytest.mark.parametrize(
    "situation",
    [
        {"households": {"your household": {"members": ["you"]}}},
        {"people": {}},
        {"people": {"you": {}}, "households": {"your household": {}}},
        {"people": {"you": {}}, "households": {"your household": {"members": []}}},
        {"people": {"you": {"age": {"2026": float("inf")}}}},
        {"people": {"you": {"age": {"2026": float("nan")}}}},
        {"people": {"you": {"age": {"2026": [40, 41]}}}},
        {"people": {"you": {}}, "axes": [[]]},
        {"people": {"you": {}}, "axes": [{"name": "age", "count": 2}]},
    ],
)
def test_household_document_rejects_malformed_situations(situation: dict) -> None:
    with pytest.raises(ValidationError):
        HouseholdDocument.model_validate(situation)


def test_axes_are_bounded_by_their_total_point_count() -> None:
    axis = {"name": "employment_income", "min": 0, "max": 1, "period": "2026"}
    within = {
        "people": {"you": {}},
        "axes": [[{**axis, "count": 100}], [{**axis, "count": 100}]],
    }
    beyond = {
        "people": {"you": {}},
        "axes": [
            [{**axis, "count": MAXIMUM_AXIS_POINTS}],
            [{**axis, "count": 2}],
        ],
    }

    assert HouseholdDocument.model_validate(within).axis_point_count() == 10_000
    with pytest.raises(ValidationError, match="axis points"):
        HouseholdDocument.model_validate(beyond)


def test_parallel_axes_must_share_one_point_count() -> None:
    axis = {"name": "employment_income", "min": 0, "max": 1}
    with pytest.raises(ValidationError, match="parallel axes"):
        HouseholdDocument.model_validate(
            {
                "people": {"you": {}, "partner": {}},
                "axes": [[{**axis, "count": 3}, {**axis, "count": 4, "index": 1}]],
            }
        )


def test_one_request_runs_baseline_and_reform_together() -> None:
    request = _request()

    assert [policy.policy_key for policy in request.policies] == [
        "baseline",
        "reform",
    ]
    assert request.schema_version == 1
    assert request.add_missing_variables is False
    assert request.spm_selection_source == "request"


def test_request_needs_at_least_one_policy_with_unique_keys() -> None:
    with pytest.raises(ValidationError):
        _request(policies=())
    with pytest.raises(ValidationError, match="unique"):
        _request(
            policies=(
                HouseholdPolicy(policy_key="reform", parameters={}),
                HouseholdPolicy(policy_key="reform", parameters={}),
            )
        )
    with pytest.raises(ValidationError):
        _request(
            policies=tuple(
                HouseholdPolicy(policy_key=f"policy-{index}", parameters={})
                for index in range(MAXIMUM_POLICIES_PER_REQUEST + 1)
            )
        )


def test_omitted_spm_inherits_defaults_and_is_not_a_choice() -> None:
    request = _request()

    assert request.spm is None
    assert request.spm_requested is False
    dumped = request.model_dump(mode="json")
    assert "spm" not in dumped
    assert HouseholdExecutionRequest.model_validate(dumped).spm_requested is False


def test_an_empty_spm_selection_is_still_a_choice() -> None:
    request = _request(spm={})

    assert request.spm_requested is True
    dumped = request.model_dump(mode="json")
    assert dumped["spm"] == {}
    assert HouseholdExecutionRequest.model_validate(dumped).spm_requested is True


def test_explicit_null_spm_is_rejected() -> None:
    with pytest.raises(ValidationError, match="omit it"):
        _request(spm=None)


def test_request_rejects_unknown_fields_and_other_schema_versions() -> None:
    with pytest.raises(ValidationError):
        _request(unexpected=True)
    with pytest.raises(ValidationError):
        _request(schema_version=2)


def test_submit_request_becomes_an_execution_request_without_losing_choices() -> None:
    submitted = SubmitHouseholdCalculationRequest.model_validate(
        {
            "country": "us",
            "household": US_SITUATION,
            "policies": [policy.model_dump(mode="json") for policy in _policies()],
            "add_missing_variables": True,
            "spm": {"geography_kind": "national"},
            "spm_selection_source": "saved_household",
            "policyengine_version": "6.7.5",
        }
    )

    request = submitted.execution_request(
        calculation_id=CALCULATION_ID, bundle=_bundle()
    )

    assert request.calculation_id == CALCULATION_ID
    assert request.household == submitted.household
    assert request.policies == submitted.policies
    assert request.add_missing_variables is True
    assert request.spm is not None
    assert request.spm.model_dump(mode="json") == {"geography_kind": "national"}
    assert request.spm_selection_source == "saved_household"
    assert request.bundle == _bundle()


def test_submit_request_rejects_a_pinned_version_the_bundle_does_not_carry() -> None:
    submitted = SubmitHouseholdCalculationRequest(
        country="us",
        household=HouseholdDocument.model_validate(US_SITUATION),
        policies=_policies(),
        policyengine_version="6.0.0",
    )

    with pytest.raises(ValueError, match="pinned"):
        submitted.execution_request(calculation_id=CALCULATION_ID, bundle=_bundle())


def test_calculated_household_keeps_axis_results_and_non_finite_strings() -> None:
    calculated = {
        "people": {
            "you": {
                "age": {"2026": [40, 40]},
                "marginal_tax_rate": {"2026": ["Infinity", 0.25]},
            }
        },
        "households": {
            "your household": {
                "members": ["you"],
                "state_name": {"2026": ["CA", "CA"]},
            }
        },
        "axes": [[{"name": "employment_income", "count": 2, "min": 0, "max": 1}]],
    }

    household = CalculatedHousehold.model_validate(calculated)

    assert household.to_situation() == calculated


def _succeeded() -> HouseholdExecutionResult:
    return HouseholdExecutionResult(
        calculation_id=CALCULATION_ID,
        output=HouseholdCalculationOutput(
            country="us",
            results=(
                HouseholdPolicyResult(
                    policy_key="baseline",
                    household=CalculatedHousehold.model_validate(
                        {
                            "people": {"you": {"age": {"2026": 40}}},
                            "households": {"your household": {"members": ["you"]}},
                        }
                    ),
                    warnings=("Input `x` was ignored.",),
                ),
            ),
            bundle=_bundle(),
        ),
        worker=_worker(),
    )


def _failed() -> HouseholdExecutionFailure:
    return HouseholdExecutionFailure(
        calculation_id=CALCULATION_ID,
        error=HouseholdCalculationError(
            code="invalid_inputs",
            message="Unrecognized calculate input(s): Unrecognized household...",
            input_errors=(
                HouseholdInputError(
                    type="household_variable",
                    name="not_a_variable",
                    path="household.people.you.not_a_variable",
                    message="Unrecognized household variable ...",
                ),
            ),
        ),
        worker=_worker(),
    )


def test_execution_outcomes_are_discriminated_by_status() -> None:
    succeeded = parse_household_execution_outcome(_succeeded().model_dump(mode="json"))
    failed = parse_household_execution_outcome(_failed().model_dump(mode="json"))

    assert isinstance(succeeded, HouseholdExecutionResult)
    assert succeeded == _succeeded()
    assert isinstance(failed, HouseholdExecutionFailure)
    assert failed == _failed()
    with pytest.raises(ValidationError):
        parse_household_execution_outcome({"status": "running"})


def test_input_errors_serialize_with_the_public_api_field_names() -> None:
    error = HouseholdInputError(
        type="household_variable_wrong_entity",
        name="state_name",
        path="household.people.you.state_name",
        message="Household variable `state_name` belongs on `households` ...",
        expected_entity_plural="households",
        actual_entity_plural="people",
    )

    assert error.model_dump(mode="json", exclude_none=True) == {
        "type": "household_variable_wrong_entity",
        "name": "state_name",
        "path": "household.people.you.state_name",
        "message": "Household variable `state_name` belongs on `households` ...",
        "expected_entity_plural": "households",
        "actual_entity_plural": "people",
    }


def test_result_policy_keys_must_be_unique() -> None:
    result = _succeeded().output.results[0]

    with pytest.raises(ValidationError, match="unique"):
        HouseholdCalculationOutput(
            country="us", results=(result, result), bundle=_bundle()
        )


def test_poll_responses_are_discriminated_by_status() -> None:
    responses = [
        PendingHouseholdCalculation(job_id=JOB_ID),
        RunningHouseholdCalculation(job_id=JOB_ID),
        SucceededHouseholdCalculation(job_id=JOB_ID, output=_succeeded().output),
        FailedHouseholdCalculation(
            job_id=JOB_ID,
            error=HouseholdCalculationError(
                code="invalid_spm_selection",
                message="SPM settings are only available for the US",
                spm_error=SPMErrorDetail(
                    code="SPM_SETTINGS_UNSUPPORTED",
                    message="SPM settings are only available for the US",
                ),
            ),
        ),
    ]

    for response in responses:
        parsed = parse_household_calculation_poll_response(
            response.model_dump(mode="json")
        )
        assert type(parsed) is type(response)
        assert parsed == response
        assert parsed.schema_version == 1
    with pytest.raises(ValidationError):
        parse_household_calculation_poll_response(
            {"status": "succeeded", "job_id": str(JOB_ID)}
        )
