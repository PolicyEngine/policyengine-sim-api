# pyright: reportAttributeAccessIssue=false
"""Separately named Stage 12 Modal coordinator and country workers.

This module defines additional resources. It does not rename or modify the
existing ``policyengine-simulation-py{version}`` applications.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from policyengine_simulation_contract.call_reports import CALL_REPORT_FUNCTION_NAME
from policyengine_simulation_contract.stage12_manifest import v2_application_name
from policyengine_simulation_executor import stage12_runtime
from policyengine_simulation_executor.hf_access_validation import (
    HF_MODAL_SECRET_NAME,
    HF_RUNTIME_ENV_NAME,
)
from policyengine_simulation_executor.stage12_bundle import (
    assert_expected_bundle_values,
    assertion_values,
    load_stage12_bundle,
)
from policyengine_simulation_executor.stage12_cache.models import CacheManifest
from policyengine_simulation_executor.stage12_cache.store import Stage12CacheStore
from policyengine_simulation_executor.stage12_timeouts import (
    COORDINATOR_TIMEOUT_SECONDS,
    UK_SIMULATION_TIMEOUT_SECONDS,
    US_SIMULATION_TIMEOUT_SECONDS,
)
from policyengine_simulation_observability.observability import (
    init_process_observability,
    modal_image_environment,
)
from policyengine_simulation_observability.stages import (
    STAGE12_CANONICAL_REPORT_STAGES,
    STAGE12_SEGMENT_STAGES,
    STAGE12_SHADOW_REPORT_STAGES,
    STAGE12_SIMULATION_STAGES,
    Stage,
)
from policyengine_simulation_observability.telemetry import (
    normalize_observability_context,
)
from src.modal.bundle_data import bundle_data_install_command
from src.modal.ca_certificates import GRPC_CA_BUNDLE_ENV, OPERATING_SYSTEM_CA_PACKAGE
from src.modal.stage12_cache_image import fetch_stage12_cache
from src.modal.static_runtime_files import add_static_runtime_files

import modal

if TYPE_CHECKING:
    from collections.abc import Callable

    from policyengine_simulation_contract.stage12_bundle import (
        CountryId,
        Stage12CountryBundle,
    )
    from policyengine_simulation_contract.stage12_execution import (
        PlannedSimulationExecutionInput,
    )
    from policyengine_simulation_executor.stage12_runtime.simulation import (
        SimulationCalculation,
    )

STAGE12_DATA_DIR = "/opt/policyengine/stage12-data"
_UV_PROJECT_DIR = str(Path(__file__).resolve().parents[2]) if modal.is_local() else "."
RESOLVED_BUNDLE = load_stage12_bundle()
BUNDLE_VALUES = assertion_values(RESOLVED_BUNDLE.bundle)
APP_NAME = v2_application_name(RESOLVED_BUNDLE.bundle.policyengine_version)


def _deploy_time_cache_inputs() -> tuple[str, dict | None]:
    """Resolve and validate the cache manifest on the deploying machine."""
    if not modal.is_local():
        return "", None
    bucket = os.environ.get("STAGE12_CACHE_BUCKET", "")
    digest = os.environ.get("STAGE12_CACHE_MANIFEST_DIGEST")
    if not digest:
        return bucket, None
    payload = Stage12CacheStore(bucket).read_manifest(digest)
    if payload is None:
        msg = "Stage 12 cache manifest is unavailable"
        raise RuntimeError(msg)
    manifest = CacheManifest.model_validate(payload)
    if manifest.bundle.bundle_manifest_sha256 != RESOLVED_BUNDLE.bundle_manifest_sha256:
        msg = "Stage 12 cache manifest names another bundle"
        raise RuntimeError(msg)
    return bucket, manifest.canonical_payload()


STAGE12_CACHE_BUCKET, STAGE12_CACHE_MANIFEST = _deploy_time_cache_inputs()


def _deploy_time_entry_url() -> str:
    """Return sim's main URL, which the deploy passes, for report_call_ended."""
    if not modal.is_local():
        return ""
    url = os.environ.get("SIMULATION_ENTRY_URL", "")
    if url and (url != url.strip() or not url.startswith("https://")):
        msg = "SIMULATION_ENTRY_URL must be an https URL"
        raise RuntimeError(msg)
    return url


SIMULATION_ENTRY_URL = _deploy_time_entry_url()
# Every attempt to report, each with its request timeout, and every wait
# between them (src/modal/call_reports.py), with a margin.
CALL_REPORT_TIMEOUT_SECONDS = 300


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
    msg = "MODAL_APP_NAME does not match the bundle-derived v2 name"
    raise RuntimeError(msg)


app = modal.App(APP_NAME)
gcp_secret = modal.Secret.from_name("stage12-evaluation-gcp-credentials")
hf_secret = modal.Secret.from_name(
    HF_MODAL_SECRET_NAME,
    required_keys=[HF_RUNTIME_ENV_NAME],
)
comparison_runtime_secret = modal.Secret.from_name("stage12-evaluation-runtime")
worker_secrets = [
    gcp_secret,
    hf_secret,
    comparison_runtime_secret,
]


def _country_bundle(country: CountryId) -> Stage12CountryBundle:
    return next(
        item for item in RESOLVED_BUNDLE.bundle.countries if item.country == country
    )


def build_v2_image(
    countries: tuple[CountryId, ...], *, include_us_cache: bool = False
) -> modal.Image:
    """Build a Stage 12 image with the bundle's certified data for ``countries``.

    The bundle manifest digest and per-country bundle values are baked into
    the image environment, which worker validation checks at run time. With
    ``include_us_cache``, the image also holds the published US cache.
    """
    country_values = {
        country: _country_bundle(country).model_dump(mode="json")
        for country in countries
    }
    image = (
        modal.Image.debian_slim(python_version="3.13")
        # Upgrade Debian's CA store before anything is downloaded, and make
        # grpcio verify servers against it (src/modal/ca_certificates.py).
        .apt_install(OPERATING_SYSTEM_CA_PACKAGE)
        .env(GRPC_CA_BUNDLE_ENV)
        .uv_sync(
            uv_project_dir=_UV_PROJECT_DIR,
            frozen=True,
            extra_options="--only-group modal-simulation-image",
            # The repository's uv release, from .uv-version.
            uv_version="0.13.6",
        )
        .run_commands(
            bundle_data_install_command(
                RESOLVED_BUNDLE.bundle.policyengine_version,
                countries=countries,
                data_dir=STAGE12_DATA_DIR,
            ),
            secrets=[hf_secret],
        )
        .env(
            {
                **modal_image_environment(),
                "POLICYENGINE_DATA_FOLDER": STAGE12_DATA_DIR,
                "STAGE12_BUNDLE_MANIFEST_SHA256": (
                    RESOLVED_BUNDLE.bundle_manifest_sha256
                ),
                "STAGE12_BUNDLE_VALUES": json.dumps(
                    country_values,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "STAGE12_CACHE_MANIFEST_DIGEST": os.environ.get(
                    "STAGE12_CACHE_MANIFEST_DIGEST", ""
                ),
            }
        )
    )
    if include_us_cache:
        image = image.run_function(
            fetch_stage12_cache,
            args=(STAGE12_CACHE_BUCKET, STAGE12_CACHE_MANIFEST),
            secrets=[gcp_secret],
            cpu=2.0,
            memory=4096,
            # The 63 national files and the state datasets (a file per state
            # for each precomputed year), downloaded and hashed in turn.
            timeout=3600,
        )
    image = image.add_local_python_source(
        "src.modal",
        "policyengine_simulation_executor",
        "policyengine_simulation_observability",
        "policyengine_simulation_contract",
        "policyengine_stage12_persistence",
        copy=True,
    )
    return add_static_runtime_files(image, uv_project_dir=_UV_PROJECT_DIR)


us_worker_image = build_v2_image(("us",), include_us_cache=True)
uk_worker_image = build_v2_image(("uk",))
coordinator_image = build_v2_image(("us", "uk"))


def _validate(country: CountryId) -> dict:
    from policyengine_simulation_executor.stage12_worker_validation import (  # noqa: PLC0415  # Modal function body: loaded in the worker container, not at deploy time
        validate_country_worker,
    )

    return validate_country_worker(
        country=country,
        expected_bundle_manifest_sha256=os.environ["STAGE12_BUNDLE_MANIFEST_SHA256"],
        expected_cache_manifest_sha256=(
            os.environ.get("STAGE12_CACHE_MANIFEST_DIGEST") if country == "us" else None
        ),
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
    """Validate the US worker image without serving and return its bundle values."""
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
    """Validate the UK worker image without serving and return its bundle values."""
    return _validate("uk")


@app.function(
    image=us_worker_image,
    cpu=8.0,
    memory=32768,
    timeout=3600,
    retries=0,
    max_containers=300,
    secrets=worker_secrets,
)
def run_single_simulation_segment_us(
    payload: dict,
    *,
    observability_context: dict | None = None,
) -> dict:
    """Calculate one US region-group segment and return its artifact descriptor."""
    runtime = init_process_observability(
        service_name="policyengine-stage12-us-segment-worker",
        service_role="stage12_segment_worker",
        platform="modal",
        environment=os.getenv("MODAL_ENVIRONMENT", "local"),
    )

    try:
        propagated = normalize_observability_context(observability_context)
        with runtime.operation(
            STAGE12_SEGMENT_STAGES.name(Stage.STAGE12_SEGMENT_EXECUTION),
            attributes={
                "runner_name": "stage12",
                "simulation_role": payload.get("simulation", {}).get("role"),
                "segment_index": payload.get("segment_index"),
            },
            remote_context=propagated,
        ):
            return stage12_runtime.calculate_segment(
                payload, runtime=runtime
            ).model_dump(mode="python")
    finally:
        runtime.shutdown()


@app.function(
    image=us_worker_image,
    cpu=8.0,
    memory=32768,
    timeout=US_SIMULATION_TIMEOUT_SECONDS,
    retries=0,
    max_containers=10,
    secrets=worker_secrets,
)
def run_single_simulation_us(
    payload: dict,
    context: dict,
    *,
    observability_context: dict | None = None,
) -> dict:
    """Calculate one planned US simulation and return its artifact descriptor."""
    runtime = init_process_observability(
        service_name="policyengine-stage12-us-worker",
        service_role="stage12_simulation_worker",
        platform="modal",
        environment=os.getenv("MODAL_ENVIRONMENT", "local"),
    )

    def segmented_calculator(
        simulation: PlannedSimulationExecutionInput,
        cancellation_requested: Callable[[], bool],
    ) -> SimulationCalculation:
        return stage12_runtime.run_segmented_simulation(
            simulation,
            app_name=APP_NAME,
            runtime=runtime,
            cancellation_requested=cancellation_requested,
        )

    try:
        propagated = normalize_observability_context(observability_context)
        with runtime.operation(
            STAGE12_SIMULATION_STAGES.name(Stage.STAGE12_SIMULATION_EXECUTION),
            attributes={
                "runner_name": "stage12",
                "simulation_role": payload.get("role"),
            },
            remote_context=propagated,
        ):
            return stage12_runtime.run_single_simulation(
                payload,
                context,
                required_country="us",
                dependencies=stage12_runtime.SimulationWorkerDependencies(
                    segmented_calculator=segmented_calculator,
                    runtime=runtime,
                ),
            )
    finally:
        runtime.shutdown()


@app.function(
    image=uk_worker_image,
    cpu=8.0,
    memory=32768,
    timeout=UK_SIMULATION_TIMEOUT_SECONDS,
    retries=0,
    max_containers=10,
    secrets=worker_secrets,
)
def run_single_simulation_uk(
    payload: dict,
    context: dict,
    *,
    observability_context: dict | None = None,
) -> dict:
    """Calculate one planned UK simulation and return its artifact descriptor."""
    runtime = init_process_observability(
        service_name="policyengine-stage12-uk-worker",
        service_role="stage12_simulation_worker",
        platform="modal",
        environment=os.getenv("MODAL_ENVIRONMENT", "local"),
    )
    try:
        propagated = normalize_observability_context(observability_context)
        with runtime.operation(
            STAGE12_SIMULATION_STAGES.name(Stage.STAGE12_SIMULATION_EXECUTION),
            attributes={
                "runner_name": "stage12",
                "simulation_role": payload.get("role"),
            },
            remote_context=propagated,
        ):
            return stage12_runtime.run_single_simulation(
                payload,
                context,
                required_country="uk",
                dependencies=stage12_runtime.SimulationWorkerDependencies(
                    runtime=runtime
                ),
            )
    finally:
        runtime.shutdown()


@app.function(
    image=coordinator_image,
    cpu=2.0,
    memory=8192,
    # The shared child-wait window (the longest simulation worker timeout),
    # the production-result wait, and time to finish and record the report.
    timeout=COORDINATOR_TIMEOUT_SECONDS,
    retries=0,
    max_containers=10,
    secrets=worker_secrets,
)
def coordinate_report(
    payload: dict,
    context: dict,
    parent: dict,
    *,
    observability_context: dict | None = None,
) -> dict:
    """Run one Stage 12 report: fan out both simulations, aggregate, and persist.

    A ``production_function_call_id`` in ``context`` marks a shadow run whose
    result is compared with that production job's. A report another
    invocation already claimed returns a ``deduplicated`` marker instead.
    """
    coordinator_invocation_id = modal.current_function_call_id()
    if coordinator_invocation_id is None:
        msg = "Modal coordinator invocation identifier is unavailable"
        raise RuntimeError(msg)
    runtime = init_process_observability(
        service_name="policyengine-stage12-coordinator",
        service_role="stage12_report_coordinator",
        platform="modal",
        environment=os.getenv("MODAL_ENVIRONMENT", "local"),
    )
    stage_plan = (
        STAGE12_SHADOW_REPORT_STAGES
        if context.get("production_function_call_id")
        else STAGE12_CANONICAL_REPORT_STAGES
    )
    try:
        propagated = normalize_observability_context(observability_context)
        with runtime.operation(
            stage_plan.name(Stage.STAGE12_COORDINATOR_EXECUTION),
            attributes={
                "runner_name": "stage12",
                "execution_mode": (
                    "shadow"
                    if context.get("production_function_call_id")
                    else "authoritative"
                ),
            },
            remote_context=propagated,
        ):
            return stage12_runtime.coordinate_report(
                payload,
                context,
                parent,
                invocation=stage12_runtime.CoordinatorInvocation(
                    application_name=APP_NAME,
                    invocation_id=coordinator_invocation_id,
                ),
                dependencies=stage12_runtime.CoordinatorDependencies(runtime=runtime),
            )
    finally:
        runtime.shutdown()


@app.function(
    image=coordinator_image,
    cpu=0.25,
    memory=512,
    timeout=CALL_REPORT_TIMEOUT_SECONDS,
    retries=0,
    max_containers=20,
    # No secrets: sim accepts this function's own Modal identity token.
    env={"SIMULATION_ENTRY_URL": SIMULATION_ENTRY_URL},
)
def report_call_ended(report: dict) -> str:
    """Tell sim that the call in ``report`` ended; return how that went.

    The v2 functions start this as their last step
    (``src.modal.call_reports.reporting_call_end``) and then return, so the
    reported call has usually returned by the time sim asks Modal for its
    outcome; when it has not, sim answers still_running and this reports
    again after a wait.
    """
    from src.modal.call_reports import (  # noqa: PLC0415  # Modal function body: loaded in the worker container, not at deploy time
        send_call_ended_report_from_container,
    )

    return send_call_ended_report_from_container(report)


if report_call_ended.__name__ != CALL_REPORT_FUNCTION_NAME:
    msg = "report_call_ended must keep the name sim accepts reports from"
    raise RuntimeError(msg)
