"""Publish a validated worker to the separate Stage 12 v2 manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import modal

from policyengine_simulation_contract.stage12_manifest import (
    V1_ROUTING_STATE_NAME,
    V2_VERSION_MANIFEST_NAME,
    V2CountryWorker,
    V2WorkerValidation,
    V2WorkerVersion,
    assert_separate_manifest_names,
    publish_v2_manifest,
    v2_application_name,
    validate_manifest_has_no_credentials,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle


def build_worker(validation_payload: object) -> V2WorkerVersion:
    resolved = load_stage12_bundle()
    validation = V2WorkerValidation.model_validate(validation_payload)
    worker = V2WorkerVersion(
        application_name=v2_application_name(resolved.bundle.policyengine_version),
        report_coordinator_callable="coordinate_report",
        countries=tuple(
            V2CountryWorker(
                country=country.country,
                single_simulation_callable=(f"run_single_simulation_{country.country}"),
            )
            for country in resolved.bundle.countries
        ),
        bundle=resolved.bundle,
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        validation=validation,
    )
    validate_manifest_has_no_credentials(worker.model_dump(mode="json"))
    return worker


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish a validated Stage 12 v2 worker manifest",
    )
    parser.add_argument("--environment", required=True)
    parser.add_argument("--validation-file", required=True, type=Path)
    parser.add_argument(
        "--manifest-name",
        default=V2_VERSION_MANIFEST_NAME,
        help="Separate Modal Dict used only for the Stage 12 v2 manifest",
    )
    parser.add_argument("--force-default", action="store_true")
    args = parser.parse_args()

    assert_separate_manifest_names(
        v1_name=V1_ROUTING_STATE_NAME,
        v2_name=args.manifest_name,
    )
    validation_payload = json.loads(args.validation_file.read_text(encoding="utf-8"))
    worker = build_worker(validation_payload)
    store = modal.Dict.from_name(
        args.manifest_name,
        environment_name=args.environment,
        create_if_missing=True,
    )
    manifest = publish_v2_manifest(
        store=store,
        worker=worker,
        force_default=args.force_default,
    )
    print(
        json.dumps(
            manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
