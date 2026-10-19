"""Invoke and verify every deployed v2 validation and household function.

Each country's validation function checks its simulation worker image, and
each country's household function calculates a small current-law household.
The release record names every invocation, so the manifest records only
functions that ran successfully in the deployed application.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import modal
from policyengine_simulation_contract.household_execution import (
    HouseholdExecutionRequest,
    HouseholdExecutionResult,
    household_callable_name,
    parse_household_execution_outcome,
)
from policyengine_simulation_contract.stage12_manifest import (
    ManifestText,
    Stage12CacheRelease,
    V2WorkerValidation,
    v2_application_name,
)
from policyengine_simulation_executor.household_calculation import (
    installed_household_bundle,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_bundle import CountryId


def _spawn_validation(
    *,
    application_name: str,
    function_name: str,
    environment: str,
    payload: dict | None = None,
) -> tuple[Any, str]:
    function = modal.Function.from_name(
        application_name,
        function_name,
        environment_name=environment,
    )
    call = function.spawn() if payload is None else function.spawn(payload)
    result = call.get(timeout=1_000)
    invocation_id = getattr(call, "object_id", None)
    if not isinstance(invocation_id, str) or not invocation_id:
        msg = f"{function_name} returned no Modal invocation identifier"
        raise RuntimeError(msg)
    return result, invocation_id


def household_validation_request(
    country: str, *, year: int
) -> HouseholdExecutionRequest:
    """Return a one-adult, current-law household request for ``country``."""
    group_plural = "spm_units" if country == "us" else "benunits"
    household: dict[str, Any] = {
        "people": {
            "you": {
                "age": {str(year): 40},
                "employment_income": {str(year): 30_000},
            }
        },
        group_plural: {"your unit": {"members": ["you"]}},
        "households": {
            "your household": {
                "members": ["you"],
                "household_net_income": {str(year): None},
            }
        },
    }
    return HouseholdExecutionRequest.model_validate(
        {
            "calculation_id": uuid4(),
            "country": country,
            "household": household,
            "policies": [{"policy_key": "baseline", "parameters": {}}],
            "bundle": installed_household_bundle(country),
        }
    )


def check_household_validation(
    outcome: object,
    *,
    request: HouseholdExecutionRequest,
    application_name: str,
    function_name: str,
) -> None:
    """Raise RuntimeError unless the household function calculated the request.

    It must succeed with this deployment's bundle, name this application and
    function, and calculate a numeric household net income.
    """
    parsed = parse_household_execution_outcome(outcome)
    if not isinstance(parsed, HouseholdExecutionResult):
        msg = f"{function_name} did not calculate the validation household"
        raise RuntimeError(msg)  # noqa: TRY004  # a failed calculation is a failed release, not a type error
    if parsed.output.bundle != request.bundle:
        msg = f"{function_name} calculated with another bundle"
        raise RuntimeError(msg)
    if (parsed.worker.application_name, parsed.worker.function_name) != (
        application_name,
        function_name,
    ):
        msg = f"{function_name} reported another function"
        raise RuntimeError(msg)
    situation = parsed.output.results[0].household.to_situation()
    net_income = situation["households"]["your household"]["household_net_income"]
    if not all(isinstance(value, float) for value in net_income.values()):
        msg = f"{function_name} returned no household net income"
        raise RuntimeError(msg)


def _validate_household_functions(
    *, application_name: str, environment: str, countries: tuple[CountryId, ...]
) -> dict[CountryId, ManifestText]:
    invocation_ids: dict[CountryId, ManifestText] = {}
    year = datetime.now(timezone.utc).year
    for country in countries:
        function_name = household_callable_name(country)
        request = household_validation_request(country, year=year)
        outcome, invocation_id = _spawn_validation(
            application_name=application_name,
            function_name=function_name,
            environment=environment,
            payload=request.model_dump(mode="json"),
        )
        check_household_validation(
            outcome,
            request=request,
            application_name=application_name,
            function_name=function_name,
        )
        invocation_ids[country] = invocation_id
    return invocation_ids


def validate_release(
    *, environment: str, expected_cache_manifest_sha256: str
) -> V2WorkerValidation:
    """Invoke each deployed validation and household function; return the record.

    Raises RuntimeError when a validation function does not report success,
    returns no invocation id, reports bundle values that differ from the local
    bundle, or reports a US cache other than ``expected_cache_manifest_sha256``,
    or when a household function does not calculate its validation household.
    """
    resolved = load_stage12_bundle()
    application_name = v2_application_name(resolved.bundle.policyengine_version)
    invocation_ids: dict[CountryId, ManifestText] = {}
    cache_release: Stage12CacheRelease | None = None
    for country_bundle in resolved.bundle.countries:
        function_name = f"validate_worker_{country_bundle.country}"
        result, invocation_id = _spawn_validation(
            application_name=application_name,
            function_name=function_name,
            environment=environment,
        )
        if not isinstance(result, dict) or result.get("validated") is not True:
            msg = f"{function_name} did not report successful validation"
            raise RuntimeError(msg)
        expected = {
            "country": country_bundle.country,
            "policyengine_version": resolved.bundle.policyengine_version,
            "country_package_name": country_bundle.country_package_name,
            "country_package_version": country_bundle.country_package_version,
            "dataset_identity": country_bundle.default_dataset,
            "dataset_uri": country_bundle.default_dataset_uri,
            "data_artifact_revision": country_bundle.data_artifact_revision,
            "bundle_manifest_sha256": resolved.bundle_manifest_sha256,
        }
        mismatches = {
            key: (result.get(key), expected_value)
            for key, expected_value in expected.items()
            if result.get(key) != expected_value
        }
        if mismatches:
            msg = f"{function_name} reported bundle values that differ from deployment"
            raise RuntimeError(msg)
        if country_bundle.country == "us":
            reported_cache = result.get("cache")
            cache_release = Stage12CacheRelease.model_validate(reported_cache)
            if cache_release.manifest_sha256 != expected_cache_manifest_sha256:
                msg = f"{function_name} reported another cache manifest"
                raise RuntimeError(msg)
        elif result.get("cache") is not None:
            msg = f"{function_name} unexpectedly reported a cache"
            raise RuntimeError(msg)
        invocation_ids[country_bundle.country] = invocation_id
    household_invocation_ids = _validate_household_functions(
        application_name=application_name,
        environment=environment,
        countries=tuple(country.country for country in resolved.bundle.countries),
    )
    return V2WorkerValidation(
        validated=True,
        application_name=application_name,
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        validated_at=datetime.now(timezone.utc),
        validation_invocation_id=f"stage12-validation-{uuid4()}",
        country_validation_invocation_ids=invocation_ids,
        household_validation_invocation_ids=household_invocation_ids,
        cache=cache_release,
    )


def main() -> None:
    """Write the release validation record to ``--output`` and print its id."""
    parser = argparse.ArgumentParser(
        description="Validate the deployed Stage 12 v2 worker release",
    )
    parser.add_argument("--environment", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache-manifest-sha256", required=True)
    args = parser.parse_args()
    validation = validate_release(
        environment=args.environment,
        expected_cache_manifest_sha256=args.cache_manifest_sha256,
    )
    args.output.write_text(
        json.dumps(
            validation.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(validation.validation_invocation_id)


if __name__ == "__main__":
    main()
