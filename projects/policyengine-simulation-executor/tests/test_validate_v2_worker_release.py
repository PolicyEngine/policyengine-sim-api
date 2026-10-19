"""Tests for deployed Stage 12 release validation."""

from __future__ import annotations

import pytest
from policyengine_simulation_contract.household_execution import (
    CalculatedHousehold,
    HouseholdCalculationOutput,
    HouseholdExecutionFailure,
    HouseholdExecutionRequest,
    HouseholdExecutionResult,
    HouseholdPolicyResult,
    HouseholdWorkerIdentity,
)
from policyengine_simulation_executor.household_calculation import (
    installed_household_bundle,
)
from src.modal.utils import validate_v2_worker_release as validation


def _household_outcome(
    payload: dict,
    *,
    application_name: str,
    function_name: str,
    succeeded: bool = True,
    bundle_overrides: dict | None = None,
) -> dict:
    request = HouseholdExecutionRequest.model_validate(payload)
    worker = HouseholdWorkerIdentity(
        application_name=application_name, function_name=function_name
    )
    if not succeeded:
        return HouseholdExecutionFailure(
            calculation_id=request.calculation_id,
            error={"code": "calculation_failed", "message": "broken"},
            worker=worker,
        ).model_dump(mode="json")
    calculated = request.household.to_situation()
    for group in calculated["households"].values():
        group["household_net_income"] = {
            period: 25_000.0 for period in group["household_net_income"]
        }
    return HouseholdExecutionResult(
        calculation_id=request.calculation_id,
        output=HouseholdCalculationOutput(
            country=request.country,
            results=(
                HouseholdPolicyResult(
                    policy_key="baseline",
                    household=CalculatedHousehold.model_validate(calculated),
                ),
            ),
            bundle=request.bundle.model_copy(update=bundle_overrides or {}),
        ),
        worker=worker,
    ).model_dump(mode="json")


def test_all_country_results_must_match_the_deployment_bundle(monkeypatch) -> None:
    invocation_ids: list[str] = []

    def fake_spawn_validation(
        *, application_name, function_name, environment, payload=None
    ):
        country = function_name.rsplit("_", maxsplit=1)[1]
        if function_name.startswith("calculate_household_"):
            invocation_id = f"household-call-{country}"
            invocation_ids.append(invocation_id)
            return (
                _household_outcome(
                    payload,
                    application_name=application_name,
                    function_name=function_name,
                ),
                invocation_id,
            )
        resolved = validation.load_stage12_bundle()
        bundle = next(
            item for item in resolved.bundle.countries if item.country == country
        )
        invocation_id = f"call-{country}"
        invocation_ids.append(invocation_id)
        return (
            {
                "validated": True,
                "country": country,
                "policyengine_version": resolved.bundle.policyengine_version,
                "country_package_name": bundle.country_package_name,
                "country_package_version": bundle.country_package_version,
                "dataset_identity": bundle.default_dataset,
                "dataset_uri": bundle.default_dataset_uri,
                "data_artifact_revision": bundle.data_artifact_revision,
                "bundle_manifest_sha256": resolved.bundle_manifest_sha256,
                "cache": (
                    {
                        "manifest_sha256": "c" * 64,
                        "partition_sha256": "d" * 64,
                        "years": [2026, 2027, 2025],
                        "segment_count": 20,
                        "dataset_count": 3,
                        "baseline_count": 60,
                    }
                    if country == "us"
                    else None
                ),
            },
            invocation_id,
        )

    monkeypatch.setattr(validation, "_spawn_validation", fake_spawn_validation)

    result = validation.validate_release(
        environment="staging",
        expected_cache_manifest_sha256="c" * 64,
    )

    assert result.validated is True
    assert result.country_validation_invocation_ids == {
        "us": "call-us",
        "uk": "call-uk",
    }
    assert result.household_validation_invocation_ids == {
        "us": "household-call-us",
        "uk": "household-call-uk",
    }
    assert invocation_ids == [
        "call-us",
        "call-uk",
        "household-call-us",
        "household-call-uk",
    ]
    assert result.cache is not None
    assert result.cache.manifest_sha256 == "c" * 64


def test_country_result_mismatch_stops_publication(monkeypatch) -> None:
    monkeypatch.setattr(
        validation,
        "_spawn_validation",
        lambda **_: (
            {
                "validated": True,
                "country": "us",
                "policyengine_version": "different",
            },
            "call-us",
        ),
    )

    with pytest.raises(RuntimeError, match="differ from deployment"):
        validation.validate_release(
            environment="staging",
            expected_cache_manifest_sha256="c" * 64,
        )


def test_validation_household_requests_current_law_net_income() -> None:
    request = validation.household_validation_request("us", year=2026)

    assert request.country == "us"
    assert request.bundle == installed_household_bundle("us")
    assert [policy.policy_key for policy in request.policies] == ["baseline"]
    situation = request.household.to_situation()
    assert situation["households"]["your household"]["household_net_income"] == {
        "2026": None
    }


@pytest.mark.parametrize(
    ("outcome_options", "message"),
    [
        ({"succeeded": False}, "did not calculate"),
        ({"bundle_overrides": {"policyengine_version": "0.0.1"}}, "another bundle"),
        ({"function_name": "calculate_household_other"}, "another function"),
    ],
)
def test_household_function_failures_stop_publication(outcome_options, message) -> None:
    application_name = validation.v2_application_name(
        validation.load_stage12_bundle().bundle.policyengine_version
    )
    request = validation.household_validation_request("uk", year=2026)
    options = {"function_name": "calculate_household_uk", **outcome_options}
    outcome = _household_outcome(
        request.model_dump(mode="json"),
        application_name=application_name,
        **options,
    )

    with pytest.raises(RuntimeError, match=message):
        validation.check_household_validation(
            outcome,
            request=request,
            application_name=application_name,
            function_name="calculate_household_uk",
        )
