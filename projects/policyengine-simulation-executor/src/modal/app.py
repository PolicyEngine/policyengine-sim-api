"""
PolicyEngine Simulation - Versioned Modal App

This app contains the heavy simulation workload with snapshotted models.
Each deployment creates a versioned app (e.g., policyengine-simulation-py4-10-0).

The gateway app (policyengine-simulation-gateway) routes requests to these versioned apps.
"""

import modal
import os
from pathlib import Path

from policyengine_observability import operation, set_attribute

from src.modal._image_setup import prebuild_country_datasets, snapshot_models
from src.modal.dependency_pins import project_dependency_pin
from policyengine_simulation_observability.logfire_legacy import (
    configure_logfire,
    flush_logfire,
    legacy_logfire_attributes,
    logfire_span,
)
from src.modal.logging_redaction import redact_params_for_logging
from policyengine_simulation_observability.observability import (
    configure_process_observability,
    init_process_observability,
    process_static_attributes,
)
from policyengine_simulation_executor.release_bundle import (
    get_bundled_country_model_version,
)


def _version_from_env_or_local_dependency(env_var: str, package: str) -> str:
    value = os.environ.get(env_var)
    if value:
        return value
    if modal.is_local():
        return project_dependency_pin(package)
    raise RuntimeError(
        f"{env_var} must be set in the Modal image environment for remote "
        "simulation workers."
    )


def _version_from_env_or_local_bundle(env_var: str, country: str) -> str:
    value = os.environ.get(env_var)
    if value:
        return value
    if modal.is_local():
        return get_bundled_country_model_version(country)
    raise RuntimeError(
        f"{env_var} must be set in the Modal image environment for remote "
        "simulation workers."
    )


POLICYENGINE_VERSION = _version_from_env_or_local_dependency(
    "POLICYENGINE_VERSION",
    "policyengine",
)
POLICYENGINE_CORE_VERSION = _version_from_env_or_local_dependency(
    "POLICYENGINE_CORE_VERSION",
    "policyengine-core",
)
US_VERSION = _version_from_env_or_local_bundle("POLICYENGINE_US_VERSION", "us")
UK_VERSION = _version_from_env_or_local_bundle("POLICYENGINE_UK_VERSION", "uk")
SIMULATION_BUNDLE_DATA_DIR = os.environ.get(
    "POLICYENGINE_BUNDLE_DATA_DIR",
    "/opt/policyengine/data",
)
SIMULATION_BUNDLE_RECEIPT = (
    f"{SIMULATION_BUNDLE_DATA_DIR}/.policyengine-bundle-receipt.json"
)
VERSION_ENV = {
    "POLICYENGINE_VERSION": POLICYENGINE_VERSION,
    "POLICYENGINE_CORE_VERSION": POLICYENGINE_CORE_VERSION,
    "POLICYENGINE_US_VERSION": US_VERSION,
    "POLICYENGINE_UK_VERSION": UK_VERSION,
    "POLICYENGINE_DATA_FOLDER": SIMULATION_BUNDLE_DATA_DIR,
    "POLICYENGINE_BUNDLE_RECEIPT": SIMULATION_BUNDLE_RECEIPT,
}


def get_app_name(policyengine_version: str) -> str:
    """
    Generate versioned app name from the policyengine.py package version.

    Replaces dots with dashes for URL safety.
    Example: 4.10.0 -> policyengine-simulation-py4-10-0
    """
    policyengine_safe = policyengine_version.replace(".", "-")
    return f"policyengine-simulation-py{policyengine_safe}"


# App name can be overridden via environment variable, otherwise generated from versions
APP_NAME = os.environ.get("MODAL_APP_NAME", get_app_name(POLICYENGINE_VERSION))

# App definition with versioned name
app = modal.App(APP_NAME)

# Secrets
# GCP credentials are shared across environments (always from main)
gcp_secret = modal.Secret.from_name("gcp-credentials", environment_name="main")
data_secret = modal.Secret.from_name("policyengine-data-credentials")
hf_secret = modal.Secret.from_name("huggingface-token")
# Legacy Logfire export remains while we evaluate a replacement observability platform.
logfire_secret = modal.Secret.from_name("policyengine-logfire")


# Only meaningful locally: image definitions are built on the deploying
# machine. Inside containers this module loads as the entrypoint at
# /root/app.py, where parents[2] does not exist (and the project dir is
# never read container-side).
_UV_PROJECT_DIR = str(Path(__file__).resolve().parents[2]) if modal.is_local() else "."


def bundle_install_command(policyengine_version: str) -> str:
    return " ".join(
        [
            "uvx",
            "--from",
            f"policyengine=={policyengine_version}",
            "policyengine",
            "bundle",
            "install",
            policyengine_version,
            # Install into uv_sync's venv so the bundle's model packages
            # share one environment with the locked bootstrap packages
            # (Modal's uv_sync creates the venv at /.uv/.venv and prepends
            # its bin to PATH). Temporary bridge: once policyengine's CLI
            # grows a datasets-only mode, uv will own all packages and
            # this step shrinks to data + receipt.
            "--venv",
            "/.uv/.venv",
            "--country",
            "us",
            "--country",
            "uk",
            "--data-dir",
            SIMULATION_BUNDLE_DATA_DIR,
            "--yes",
        ]
    )


def build_runtime_simulation_image() -> modal.Image:
    """Image layers up to the version env — everything except the dataset
    prebuild and model snapshot.

    Shared by the deployed app, the prewarm app, and the image smoke app
    (src/modal/smoke_app.py): all must construct these layers through
    this one code path so their definitions — and therefore Modal's
    content-addressed layer cache keys — are identical.
    """
    return (
        modal.Image.debian_slim(python_version="3.13")
        # The modal-simulation-image dependency group, installed straight
        # from this project's uv.lock (frozen): image packages match the
        # tested environment and can only change through a relock.
        # --only-group keeps the heavyweight project dependencies out —
        # country models arrive via the policyengine bundle install below.
        .uv_sync(
            uv_project_dir=_UV_PROJECT_DIR,
            frozen=True,
            extra_options="--only-group modal-simulation-image",
        )
        .run_commands(
            bundle_install_command(POLICYENGINE_VERSION),
            secrets=[data_secret, hf_secret],
        )
        .env(VERSION_ENV)
    )


def build_base_simulation_image() -> modal.Image:
    """Runtime image layers plus the dataset prebuild."""
    return (
        build_runtime_simulation_image()
        # TEMPORARY: remove once single-year datasets are published (issue
        # #596). Prebuild US single-year datasets into the image so cold
        # containers skip the slow runtime build. US only for now, to keep
        # image build time low — UK requests still build at request time.
        # This layer MUST stay before add_local_python_source — that layer
        # is keyed on source file hashes, so anything after it rebuilds on
        # every code change, and this layer takes hours. To force a rebuild
        # of a cached layer (e.g. after a data re-release under the same
        # revision), temporarily add force_build=True.
        .run_function(
            prebuild_country_datasets,
            args=("us",),
            secrets=[data_secret, hf_secret],
            cpu=8.0,
            memory=65536,
            timeout=4 * 60 * 60,
        )
    )


# Heavy image with model snapshot for simulation
simulation_image = (
    build_base_simulation_image()
    .add_local_python_source(
        "src.modal",
        "policyengine_simulation_executor",
        "policyengine_simulation_observability",
        "policyengine_simulation_contract",
        copy=True,
    )
    .run_function(snapshot_models)
)


def _configure_modal_observability(
    *,
    service_role: str,
    modal_function_name: str,
) -> dict:
    configure_process_observability(
        platform="modal",
        service_role=service_role,
        modal_app_name=APP_NAME,
        modal_function_name=modal_function_name,
    )
    init_process_observability(service_role=service_role)
    # Worker operations have no FastAPI adapter to inject static identity
    # attributes, so the caller must merge these into its operation attrs.
    return process_static_attributes(service_role=service_role)


def _set_modal_call_attributes() -> None:
    try:
        set_attribute("function_call_id", modal.current_function_call_id())
    except Exception:
        pass


@app.function(
    image=simulation_image,
    cpu=8.0,
    memory=32768,
    timeout=3600,
    retries=0,
    max_containers=100,
    secrets=[gcp_secret, data_secret, hf_secret, logfire_secret],
)
def run_simulation(params: dict) -> dict:
    """
    Execute economic simulation.

    Imports the snapshotted implementation at runtime.
    Emits redacted operation data to both observability systems.
    """
    static_attributes = _configure_modal_observability(
        service_role="simulation_worker",
        modal_function_name="run_simulation",
    )

    # We deliberately avoid sending full ``params`` or ``result`` blobs to
    # either observability system: both can embed signed URLs, reform
    # parameter trees with sensitive policy details, or result payloads
    # large enough to blow attribute budgets. The redacted summary keeps
    # correlation traceability via run_id while leaving the heavy payload
    # in memory.
    redacted_params = {
        **redact_params_for_logging(params),
        **static_attributes,
        **legacy_logfire_attributes(),
    }
    logfire_enabled = False
    try:
        with operation("run_simulation", flavor="modal_function", **redacted_params):
            logfire_enabled = configure_logfire("policyengine-simulation")
            _set_modal_call_attributes()
            with logfire_span(logfire_enabled, "run_simulation", **redacted_params):
                from src.modal.segmented_national import (
                    dispatch_run_simulation,
                )

                # Plain national macro requests fan out across region groups
                # by default (segmented: false opts out). APP_NAME is this
                # deployed app, so children spawn into the same worker pool.
                return dispatch_run_simulation(params, app_name=APP_NAME)
    finally:
        flush_logfire(logfire_enabled)


@app.function(
    image=simulation_image,
    cpu=1.0,
    memory=4096,
    timeout=3600,
    retries=0,
    max_containers=100,
    secrets=[gcp_secret, data_secret, hf_secret, logfire_secret],
)
def run_budget_window_batch(params: dict) -> dict:
    """Execute a multi-year budget-window batch orchestration."""
    static_attributes = _configure_modal_observability(
        service_role="budget_window_worker",
        modal_function_name="run_budget_window_batch",
    )

    redacted_params = {
        **redact_params_for_logging(params),
        **static_attributes,
        **legacy_logfire_attributes(),
    }
    logfire_enabled = False
    try:
        with operation(
            "run_budget_window_batch",
            flavor="modal_function",
            **redacted_params,
        ):
            logfire_enabled = configure_logfire("policyengine-simulation")
            _set_modal_call_attributes()
            with logfire_span(
                logfire_enabled,
                "run_budget_window_batch",
                **redacted_params,
            ):
                from src.modal.budget_window_batch import run_budget_window_batch_impl

                return run_budget_window_batch_impl(params)
    finally:
        flush_logfire(logfire_enabled)
