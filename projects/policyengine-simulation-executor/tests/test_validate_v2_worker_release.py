"""Tests for deployed Stage 12 release validation."""

from __future__ import annotations

import pytest
from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionRequest,
)
from src.modal.utils import validate_v2_worker_release as validation


def _household_outcome(
    request: HouseholdExecutionRequest, *, application_name: str, function_name: str
) -> dict:
    """Return the success a household function reports for ``request``."""
    situation = request.household.to_situation()
    for period in situation["households"]["your household"]["household_net_income"]:
        situation["households"]["your household"]["household_net_income"][period] = (
            25_000.0
        )
    return {
        "status": "succeeded",
        "schema_version": 1,
        "calculation_id": str(request.calculation_id),
        "output": {
            "country": request.country,
            "bundle": request.bundle.model_dump(mode="json"),
            "results": [{"policy_key": "baseline", "household": situation}],
        },
        "worker": {
            "application_name": application_name,
            "function_name": function_name,
        },
    }


class HouseholdFunctions:
    """Stands in for the deployed household functions; records each call."""

    def __init__(self, change_outcome=None) -> None:
        self.calls: list[tuple[str, HouseholdExecutionRequest]] = []
        self.change_outcome = change_outcome or (lambda outcome: outcome)

    def __call__(self, *, application_name, function_name, environment, payload):
        assert environment == "staging"
        request = HouseholdExecutionRequest.model_validate(payload)
        self.calls.append((function_name, request))
        outcome = _household_outcome(
            request, application_name=application_name, function_name=function_name
        )
        return self.change_outcome(outcome), f"household-{request.country}"


def test_all_country_results_must_match_the_deployment_bundle(monkeypatch) -> None:
    invocation_ids: list[str] = []

    def fake_spawn_validation(*, application_name, function_name, environment):
        country = function_name.rsplit("_", maxsplit=1)[1]
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
    household_functions = HouseholdFunctions()
    monkeypatch.setattr(validation, "_call_household_function", household_functions)

    result = validation.validate_release(
        environment="staging",
        expected_cache_manifest_sha256="c" * 64,
    )

    assert result.validated is True
    assert result.country_validation_invocation_ids == {
        "us": "call-us",
        "uk": "call-uk",
    }
    assert invocation_ids == ["call-us", "call-uk"]
    assert result.cache is not None
    assert result.cache.manifest_sha256 == "c" * 64
    # Each household function calculated a current-law household with the
    # deployment's bundle, and the record names each invocation.
    assert [name for name, _ in household_functions.calls] == [
        "calculate_household_us",
        "calculate_household_uk",
    ]
    assert result.household_validation_invocation_ids == {
        "us": "household-us",
        "uk": "household-uk",
    }


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


def _fail(outcome: dict) -> dict:
    return {
        "status": "failed",
        "schema_version": 1,
        "calculation_id": outcome["calculation_id"],
        "error": {"code": "calculation_failed", "message": "model exploded"},
        "worker": outcome["worker"],
    }


def test_a_household_function_that_does_not_calculate_stops_publication(
    monkeypatch,
) -> None:
    household_functions = HouseholdFunctions(_fail)
    monkeypatch.setattr(validation, "_call_household_function", household_functions)

    with pytest.raises(
        RuntimeError,
        match="calculate_household_us did not calculate the validation household",
    ):
        validation.validate_household_functions(
            application_name="policyengine-simulation-v2-py-test",
            environment="staging",
            year=2026,
        )

    assert [name for name, _ in household_functions.calls] == ["calculate_household_us"]
