# pyright: reportAttributeAccessIssue=false
"""Separately named Stage 12 Modal coordinator and country workers.

This module defines additional resources. It does not rename or modify the
existing ``policyengine-simulation-py{version}`` applications.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from policyengine_simulation_contract.stage12_bundle import CountryId
from policyengine_simulation_contract.stage12_manifest import v2_application_name

import modal
from policyengine_simulation_executor.stage12_bundle import (
    assert_expected_bundle_values,
    assertion_values,
    load_stage12_bundle,
)

STAGE12_DATA_DIR = "/opt/policyengine/stage12-data"
_UV_PROJECT_DIR = str(Path(__file__).resolve().parents[2]) if modal.is_local() else "."
RESOLVED_BUNDLE = load_stage12_bundle()
BUNDLE_VALUES = assertion_values(RESOLVED_BUNDLE.bundle)
APP_NAME = v2_application_name(RESOLVED_BUNDLE.bundle.policyengine_version)


def _external_assertions(environment: dict[str, str]) -> dict[str, str]:
    mapping = {
        "STAGE12_EXPECT_POLICYENGINE_VERSION": "policyengine_version",
        "STAGE12_EXPECT_US_VERSION": "countries.us.country_package_version",
        "STAGE12_EXPECT_UK_VERSION": "countries.uk.country_package_version",
        "STAGE12_EXPECT_US_DATASET": "countries.us.default_dataset",
        "STAGE12_EXPECT_UK_DATASET": "countries.uk.default_dataset",
    }
    return {
        assertion: environment[name]
        for name, assertion in mapping.items()
        if environment.get(name)
    }


assert_expected_bundle_values(
    RESOLVED_BUNDLE.bundle,
    _external_assertions(dict(os.environ)),
)
if os.environ.get("MODAL_APP_NAME") not in {None, "", APP_NAME}:
    raise RuntimeError("MODAL_APP_NAME does not match the bundle-derived v2 name")


app = modal.App(APP_NAME)
gcp_secret = modal.Secret.from_name("stage12-evaluation-gcp-credentials")
data_secret = modal.Secret.from_name("policyengine-data-credentials")
hf_secret = modal.Secret.from_name("huggingface-token")
logfire_secret = modal.Secret.from_name("policyengine-logfire")
comparison_runtime_secret = modal.Secret.from_name("stage12-evaluation-runtime")
worker_secrets = [
    gcp_secret,
    data_secret,
    hf_secret,
    logfire_secret,
    comparison_runtime_secret,
]


def _country_bundle(country: CountryId):
    return next(
        item for item in RESOLVED_BUNDLE.bundle.countries if item.country == country
    )


def bundle_install_command(countries: tuple[CountryId, ...]) -> str:
    version = RESOLVED_BUNDLE.bundle.policyengine_version
    parts = [
        "uvx",
        "--from",
        RESOLVED_BUNDLE.bundle.policyengine_requirement,
        "policyengine",
        "bundle",
        "install",
        version,
        "--venv",
        "/.uv/.venv",
    ]
    for country in countries:
        parts.extend(("--country", country))
    parts.extend(("--data-dir", STAGE12_DATA_DIR, "--yes"))
    return " ".join(shlex.quote(part) for part in parts)


def build_v2_image(countries: tuple[CountryId, ...]) -> modal.Image:
    country_values = {
        country: _country_bundle(country).model_dump(mode="json")
        for country in countries
    }
    return (
        modal.Image.debian_slim(python_version="3.13")
        .uv_sync(
            uv_project_dir=_UV_PROJECT_DIR,
            frozen=True,
            extra_options="--only-group modal-simulation-image",
        )
        .run_commands(
            bundle_install_command(countries), secrets=[data_secret, hf_secret]
        )
        .env(
            {
                "POLICYENGINE_DATA_FOLDER": STAGE12_DATA_DIR,
                "STAGE12_BUNDLE_MANIFEST_SHA256": (
                    RESOLVED_BUNDLE.bundle_manifest_sha256
                ),
                "STAGE12_BUNDLE_VALUES": json.dumps(
                    country_values,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
        .add_local_python_source(
            "src.modal",
            "policyengine_simulation_executor",
            "policyengine_simulation_observability",
            "policyengine_simulation_contract",
            copy=True,
        )
    )


us_worker_image = build_v2_image(("us",))
uk_worker_image = build_v2_image(("uk",))
coordinator_image = build_v2_image(("us", "uk"))


def _validate(country: CountryId) -> dict:
    from policyengine_simulation_executor.stage12_worker_validation import (
        validate_country_worker,
    )

    return validate_country_worker(
        country=country,
        expected_bundle_manifest_sha256=os.environ["STAGE12_BUNDLE_MANIFEST_SHA256"],
    )


@app.function(
    image=us_worker_image,
    cpu=2.0,
    memory=4096,
    timeout=900,
    retries=0,
    secrets=worker_secrets,
)
def validate_worker_us() -> dict:
    return _validate("us")


@app.function(
    image=uk_worker_image,
    cpu=2.0,
    memory=4096,
    timeout=900,
    retries=0,
    secrets=worker_secrets,
)
def validate_worker_uk() -> dict:
    return _validate("uk")


@app.function(
    image=us_worker_image,
    cpu=8.0,
    memory=32768,
    timeout=3000,
    retries=0,
    max_containers=10,
    secrets=worker_secrets,
)
def run_single_simulation_us(payload: dict, context: dict) -> dict:
    from policyengine_simulation_executor.stage12_runtime import (
        run_single_simulation,
    )

    return run_single_simulation(payload, context, required_country="us")


@app.function(
    image=uk_worker_image,
    cpu=8.0,
    memory=32768,
    timeout=3000,
    retries=0,
    max_containers=10,
    secrets=worker_secrets,
)
def run_single_simulation_uk(payload: dict, context: dict) -> dict:
    from policyengine_simulation_executor.stage12_runtime import (
        run_single_simulation,
    )

    return run_single_simulation(payload, context, required_country="uk")


@app.function(
    image=coordinator_image,
    cpu=2.0,
    memory=8192,
    # Allow one 50-minute child-calculation window, the subsequent 15-minute
    # production-result wait, and bounded aggregation/persistence overhead.
    timeout=4500,
    retries=0,
    max_containers=10,
    secrets=worker_secrets,
)
def coordinate_report(payload: dict, context: dict, parent: dict) -> dict:
    from policyengine_simulation_executor.stage12_runtime import (
        coordinate_report as run_report_coordinator,
    )

    coordinator_invocation_id = modal.current_function_call_id()
    if coordinator_invocation_id is None:
        raise RuntimeError("Modal coordinator invocation identifier is unavailable")
    return run_report_coordinator(
        payload,
        context,
        parent,
        application_name=APP_NAME,
        coordinator_invocation_id=coordinator_invocation_id,
    )
