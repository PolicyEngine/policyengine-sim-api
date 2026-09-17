"""Invoke and verify every deployed Stage 12 country validation function."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import modal

from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_contract.stage12_manifest import (
    ManifestText,
    V2WorkerValidation,
    v2_application_name,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle


def _spawn_validation(
    *,
    application_name: str,
    function_name: str,
    environment: str,
) -> tuple[Any, str]:
    function = modal.Function.from_name(
        application_name,
        function_name,
        environment_name=environment,
    )
    call = function.spawn()
    result = call.get(timeout=1_000)
    invocation_id = getattr(call, "object_id", None)
    if not isinstance(invocation_id, str) or not invocation_id:
        raise RuntimeError(f"{function_name} returned no Modal invocation identifier")
    return result, invocation_id


def validate_release(*, environment: str) -> V2WorkerValidation:
    resolved = load_stage12_bundle()
    application_name = v2_application_name(resolved.bundle.policyengine_version)
    invocation_ids: dict[CountryId, ManifestText] = {}
    for country_bundle in resolved.bundle.countries:
        function_name = f"validate_worker_{country_bundle.country}"
        result, invocation_id = _spawn_validation(
            application_name=application_name,
            function_name=function_name,
            environment=environment,
        )
        if not isinstance(result, dict) or result.get("validated") is not True:
            raise RuntimeError(f"{function_name} did not report successful validation")
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
            raise RuntimeError(
                f"{function_name} reported bundle values that differ from deployment"
            )
        invocation_ids[country_bundle.country] = invocation_id
    return V2WorkerValidation(
        validated=True,
        application_name=application_name,
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        validated_at=datetime.now(timezone.utc),
        validation_invocation_id=f"stage12-validation-{uuid4()}",
        country_validation_invocation_ids=invocation_ids,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate the deployed Stage 12 v2 worker release",
    )
    parser.add_argument("--environment", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    validation = validate_release(environment=args.environment)
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
