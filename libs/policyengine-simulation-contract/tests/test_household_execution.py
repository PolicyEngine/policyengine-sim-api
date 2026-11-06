"""Tests for the versioned household calculation contract."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from policyengine_simulation_contract.household_execution import (
    MAXIMUM_AXIS_POINTS,
    MAXIMUM_POLICIES_PER_REQUEST,
    CalculatedHousehold,
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
    household_callable_name,
    parse_household_execution_outcome,
)
from policyengine_simulation_contract.spm import (
    SPMErrorDetail,
    SPMResolvedConfiguration,
    SPMRuntimeVersions,
    SPMSelection,
    build_spm_provenance,
)
from policyengine_simulation_contract.stage12_bundle import (
    Stage12BundleManifest,
    Stage12CountryBundle,
    Stage12Dataset,
)
from pydantic import ValidationError

CALCULATION_ID = UUID("00000000-0000-4000-8000-0000000000c1")

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
# The same household with its top-level keys, and one group's keys, in
# another order.
REORDERED_SITUATION = {
    "households": {
        "your household": {
            "state_name": {"2026": "CA"},
            "members": ["you", "your partner"],
            "household_net_income": {"2026": None},
        }
    },
    "axes": US_SITUATION["axes"],
    "people": US_SITUATION["people"],
    "tax_units": US_SITUATION["tax_units"],
}
CTC_REFORM = {
    "gov.irs.credits.ctc.amount.base[0].amount": {"2026-01-01.2100-12-31": 3_000}
}
SPM_RUNTIME_VERSIONS = SPMRuntimeVersions.model_validate(
    {
        "policyengine": "0.0.0-test-policyengine",
        "policyengine-core": "0.0.0-test-policyengine-core",
        "policyengine-us": "0.0.0-test-policyengine-us",
        "spm-calculator": "0.0.0-test-spm-calculator",
    }
)
RESOLVED_SPM_SELECTION = SPMSelection(
    forecast_content_sha256="c" * 64,
    scenario="ce_trend",
    geography_kind="national",
    geography_id=None,
    county_vintage="2020",
    as_of=None,
)


def _bundle() -> HouseholdBundleProvenance:
    return HouseholdBundleProvenance(
        policyengine_version="6.7.5",
        core_package_version="3.33.0",
        country_package_name="policyengine-us",
        country_package_version="2.2.1",
        bundle_manifest_sha256="a" * 64,
    )


def _policies() -> list[dict[str, object]]:
    return [
        {"policy_key": "baseline", "parameters": {}},
        {"policy_key": "reform", "parameters": CTC_REFORM},
    ]


def _request(**overrides: object) -> HouseholdExecutionRequest:
    values: dict[str, object] = {
        "calculation_id": str(CALCULATION_ID),
        "country": "us",
        "year": "2026",
        "household": US_SITUATION,
        "policies": _policies(),
        "bundle": _bundle().model_dump(mode="json"),
    }
    values.update(overrides)
    return HouseholdExecutionRequest.model_validate(values)


def _worker() -> HouseholdWorkerIdentity:
    return HouseholdWorkerIdentity(
        application_name="policyengine-simulation-v2-py6-7-5",
        function_name="calculate_household_us",
    )


def _calculated(situation: dict) -> CalculatedHousehold:
    return CalculatedHousehold.model_validate(situation)


def _output(**overrides: object) -> HouseholdCalculationOutput:
    values: dict[str, object] = {
        "country": "us",
        "bundle": _bundle(),
        "results": (
            HouseholdPolicyResult(
                policy_key="baseline",
                household=_calculated(
                    {
                        "people": {"you": {"age": {"2026": 40}}},
                        "households": {"your household": {"members": ["you"]}},
                    }
                ),
                warnings=("Input `x` was ignored.",),
            ),
        ),
    }
    values.update(overrides)
    return HouseholdCalculationOutput.model_validate(values)


def test_household_document_keeps_the_public_api_situation_shape() -> None:
    document = HouseholdDocument.model_validate(US_SITUATION)

    assert document.to_situation() == US_SITUATION
    assert document.has_axes
    assert document.axis_point_count() == 11


def test_households_keep_the_callers_key_order_through_transport() -> None:
    # The Public API walks a household in the order its caller sent it, so the
    # order of unrecognized inputs, warnings and calculated values follows it.
    request = _request(household=REORDERED_SITUATION)
    transported = HouseholdExecutionRequest.model_validate(
        json.loads(request.model_dump_json())
    )

    for situation in (
        request.household.to_situation(),
        transported.household.to_situation(),
        json.loads(transported.household.model_dump_json()),
        _calculated(REORDERED_SITUATION).to_situation(),
    ):
        assert list(situation) == list(REORDERED_SITUATION)
        assert list(situation["households"]["your household"]) == [
            "state_name",
            "members",
            "household_net_income",
        ]
        assert situation == REORDERED_SITUATION


def test_a_household_without_axes_never_gains_an_axes_key() -> None:
    situation = {key: value for key, value in US_SITUATION.items() if key != "axes"}

    document = HouseholdDocument.model_validate(situation)

    assert not document.has_axes
    assert document.axis_point_count() == 1
    assert document.to_situation() == situation
    assert "axes" not in json.loads(document.model_dump_json())


def test_axes_keep_the_values_and_keys_the_caller_sent() -> None:
    situation = {
        "people": {"you": {}},
        "axes": [[{"name": "employment_income", "count": 3, "min": 0, "max": 2.5}]],
    }

    assert HouseholdDocument.model_validate(situation).to_situation() == situation


def test_a_household_accepts_values_without_periods() -> None:
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
        {
            "people": {"you": {}},
            "households": {"your household": {"members": ["you", "you"]}},
        },
        {"people": {"you": {"age": {"2026": float("inf")}}}},
        {"people": {"you": {"age": {"2026": float("nan")}}}},
        {"people": {"you": {"age": {"2026": [40, 41]}}}},
        {"people": {"you": {}}, "axes": [[]]},
        {"people": {"you": {}}, "axes": [{"name": "age", "count": 2}]},
        {"people": {"you": {}}, "axes": [[{"name": "age", "count": 0}]]},
    ],
)
def test_a_household_rejects_malformed_situations(situation: dict) -> None:
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
        "axes": [[{**axis, "count": MAXIMUM_AXIS_POINTS}], [{**axis, "count": 2}]],
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


def test_one_request_carries_the_baseline_and_its_reform() -> None:
    request = _request()

    assert [policy.policy_key for policy in request.policies] == ["baseline", "reform"]
    assert request.policies[1].parameters == CTC_REFORM
    assert request.schema_version == 1
    assert request.add_missing_variables is False
    assert request.spm_selection_source == "request"
    assert request.bundle == _bundle()


def test_a_request_names_the_year_it_calculates() -> None:
    request = _request(year="2025")

    assert request.year == "2025"
    assert request.model_dump(mode="json")["year"] == "2025"
    assert (
        HouseholdExecutionRequest.model_validate(request.model_dump(mode="json"))
        == request
    )


def test_a_request_without_a_year_is_refused() -> None:
    values = _request().model_dump(mode="json")
    del values["year"]

    with pytest.raises(ValidationError, match="year"):
        HouseholdExecutionRequest.model_validate(values)


@pytest.mark.parametrize("year", ["25", "20255", "2025-01", "year", "", " 2025", 2025])
def test_a_request_with_a_malformed_year_is_refused(year: object) -> None:
    with pytest.raises(ValidationError, match="year"):
        _request(year=year)


def test_add_missing_variables_asks_for_a_full_calculation() -> None:
    assert _request(add_missing_variables=True).add_missing_variables is True


def test_a_request_needs_one_to_ten_policies_with_unique_keys() -> None:
    eleven_policies = [
        {"policy_key": f"policy-{index}", "parameters": {}}
        for index in range(MAXIMUM_POLICIES_PER_REQUEST + 1)
    ]
    repeated_key = [
        {"policy_key": "reform", "parameters": {}},
        {"policy_key": "reform", "parameters": CTC_REFORM},
    ]

    assert len(_request(policies=eleven_policies[:10]).policies) == 10
    with pytest.raises(ValidationError):
        _request(policies=[])
    with pytest.raises(ValidationError):
        _request(policies=eleven_policies)
    with pytest.raises(ValidationError, match="unique"):
        _request(policies=repeated_key)
    with pytest.raises(ValidationError):
        _request(policies=[{"policy_key": "Not A Key", "parameters": {}}])


def test_omitted_spm_inherits_the_defaults_and_is_not_a_choice() -> None:
    request = _request()

    assert request.spm is None
    assert request.spm_requested is False
    dumped = request.model_dump(mode="json")
    assert "spm" not in dumped
    assert HouseholdExecutionRequest.model_validate(dumped).spm_requested is False


def test_an_empty_spm_selection_is_still_a_choice() -> None:
    request = _request(spm={}, spm_selection_source="saved_household")

    assert request.spm_requested is True
    dumped = request.model_dump(mode="json")
    assert dumped["spm"] == {}
    assert dumped["spm_selection_source"] == "saved_household"
    assert HouseholdExecutionRequest.model_validate(dumped).spm_requested is True


def test_spm_settings_travel_exactly_as_the_caller_sent_them() -> None:
    # The household function checks them, so a malformed selection or an
    # explicit null fails with the Public API's SPM error codes.
    for sent in (None, {"geography_kind": "planet"}, "national"):
        request = _request(spm=sent)

        assert request.spm == sent
        assert request.spm_requested is True
        dumped = request.model_dump(mode="json")
        assert dumped["spm"] == sent
        transported = HouseholdExecutionRequest.model_validate(dumped)
        assert transported.spm == sent
        assert transported.spm_requested is True


def test_a_request_rejects_unknown_fields_and_other_schema_versions() -> None:
    with pytest.raises(ValidationError):
        _request(unexpected=True)
    with pytest.raises(ValidationError):
        _request(schema_version=2)
    with pytest.raises(ValidationError):
        _request(country="ca")


def test_a_calculated_household_keeps_axis_results_and_non_finite_strings() -> None:
    calculated = {
        "people": {
            "you": {
                "age": {"2026": [40, 40]},
                "marginal_tax_rate": {"2026": ["Infinity", 0.25]},
                "is_blind": {"2026": [False, False]},
            }
        },
        "households": {
            "your household": {
                "members": ["you"],
                "state_name": {"2026": ["CA", "CA"]},
                "spm_unit_net_income": {"2026": [None, None]},
            }
        },
        "axes": [[{"name": "employment_income", "count": 2, "min": 0, "max": 1}]],
    }

    assert _calculated(calculated).to_situation() == calculated


def test_execution_outcomes_are_told_apart_by_status() -> None:
    succeeded = HouseholdExecutionResult(
        calculation_id=CALCULATION_ID, output=_output(), worker=_worker()
    )
    failed = HouseholdExecutionFailure(
        calculation_id=CALCULATION_ID,
        error=HouseholdCalculationError(
            code="invalid_spm_selection",
            message="SPM settings are only available for the US",
            spm_error=SPMErrorDetail(
                code="SPM_SETTINGS_UNSUPPORTED",
                message="SPM settings are only available for the US",
            ),
        ),
        worker=_worker(),
    )

    parsed_success = parse_household_execution_outcome(
        succeeded.model_dump(mode="json")
    )
    parsed_failure = parse_household_execution_outcome(failed.model_dump(mode="json"))

    assert isinstance(parsed_success, HouseholdExecutionResult)
    assert parsed_success == succeeded
    assert parsed_success.schema_version == 1
    assert isinstance(parsed_failure, HouseholdExecutionFailure)
    assert parsed_failure == failed
    with pytest.raises(ValidationError):
        parse_household_execution_outcome({"status": "running"})


def test_input_errors_serialize_as_the_public_api_lists_them() -> None:
    wrong_entity = HouseholdInputError(
        type="household_variable_wrong_entity",
        name="state_name",
        path="household.people.you.state_name",
        message="Household variable `state_name` belongs on `households` ...",
        expected_entity_plural="households",
        actual_entity_plural="people",
    )
    unknown = HouseholdInputError(
        type="household_variable",
        name="not_a_variable",
        path="household.people.you.not_a_variable",
        message="Unrecognized household variable ...",
    )

    assert wrong_entity.model_dump(mode="json") == {
        "type": "household_variable_wrong_entity",
        "name": "state_name",
        "path": "household.people.you.state_name",
        "message": "Household variable `state_name` belongs on `households` ...",
        "expected_entity_plural": "households",
        "actual_entity_plural": "people",
    }
    # Like the Public API's error entries, an error names entity plurals only
    # for a variable on the wrong entity.
    assert unknown.model_dump(mode="json") == {
        "type": "household_variable",
        "name": "not_a_variable",
        "path": "household.people.you.not_a_variable",
        "message": "Unrecognized household variable ...",
    }
    assert HouseholdInputError.model_validate(unknown.model_dump()) == unknown


def test_output_results_must_have_unique_policy_keys() -> None:
    result = _output().results[0]

    with pytest.raises(ValidationError, match="unique"):
        _output(results=(result, result))


def test_output_carries_the_applied_spm_selection_and_each_receipt() -> None:
    receipt = build_spm_provenance(
        forecast_id="forecast-2026",
        forecast_sha256="c" * 64,
        selection=RESOLVED_SPM_SELECTION,
        years=("2026",),
        runtime_versions=SPM_RUNTIME_VERSIONS,
    )
    applied = SPMResolvedConfiguration.model_validate(
        RESOLVED_SPM_SELECTION.model_dump(mode="json")
    )
    result = _output().results[0].model_copy(update={"spm_provenance": receipt})

    output = _output(results=(result,), spm_config=applied)
    transported = HouseholdCalculationOutput.model_validate(
        json.loads(output.model_dump_json())
    )

    assert transported.spm_config == applied
    assert transported.results[0].spm_provenance == receipt
    assert transported.model_dump(mode="json")["spm_config"] == {
        "forecast_content_sha256": "c" * 64,
        "scenario": "ce_trend",
        "geography_kind": "national",
        "geography_id": None,
        "county_vintage": "2020",
        "as_of": None,
    }
    assert _output().spm_config is None


def test_bundle_provenance_is_read_from_a_normalized_bundle() -> None:
    dataset = Stage12Dataset(
        identity="populace_uk_2023",
        uri="hf://policyengine/data/populace_uk_2023.h5@r1",
        artifact_revision="r1",
        sha256="c" * 64,
        repo_type="dataset",
    )
    bundle = Stage12BundleManifest(
        policyengine_version="6.7.5",
        policyengine_requirement="policyengine==6.7.5",
        core_package_version="3.33.0",
        core_package_requirement="policyengine-core==3.33.0",
        countries=(
            Stage12CountryBundle(
                country="uk",
                country_package_name="policyengine-uk",
                country_package_version="2.130.1",
                country_package_requirement="policyengine-uk==2.130.1",
                data_package_name="populace-data",
                data_package_version="0.1.0",
                data_release_version="r1",
                data_artifact_revision="r1",
                default_dataset="populace_uk_2023",
                default_dataset_uri=dataset.uri,
                datasets=(dataset,),
            ),
        ),
    )

    provenance = HouseholdBundleProvenance.from_bundle(
        bundle, bundle_manifest_sha256="d" * 64, country="uk"
    )

    assert provenance.model_dump() == {
        "policyengine_version": "6.7.5",
        "core_package_version": "3.33.0",
        "country_package_name": "policyengine-uk",
        "country_package_version": "2.130.1",
        "bundle_manifest_sha256": "d" * 64,
    }
    with pytest.raises(ValueError, match="does not carry"):
        HouseholdBundleProvenance.from_bundle(
            bundle, bundle_manifest_sha256="d" * 64, country="us"
        )


def test_household_functions_are_named_by_country() -> None:
    assert household_callable_name("us") == "calculate_household_us"
    assert household_callable_name("uk") == "calculate_household_uk"


def test_policy_values_may_be_lists_for_list_parameters() -> None:
    policy = HouseholdPolicy.model_validate(
        {
            "policy_key": "reform",
            "parameters": {
                "gov.usda.snap.income.sources.earned": {
                    "2026-01-01.2100-12-31": ["employment_income"]
                }
            },
        }
    )

    assert policy.parameters["gov.usda.snap.income.sources.earned"] == {
        "2026-01-01.2100-12-31": ["employment_income"]
    }
