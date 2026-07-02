"""
PolicyEngine Simulation - Versioned Modal App

This app contains the heavy simulation workload with snapshotted models.
Each deployment creates a versioned app (e.g., policyengine-simulation-py4-10-0).

The gateway app (policyengine-simulation-gateway) routes requests to these versioned apps.
"""

import modal
import os

from src.modal._image_setup import prebuild_country_datasets, snapshot_models
from src.modal.dependency_pins import project_dependency_pin
from src.modal.logging_redaction import redact_params_for_logging
from policyengine_api_simulation.release_bundle import get_bundled_country_model_version


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
# Logfire secret is environment-specific
logfire_secret = modal.Secret.from_name("policyengine-logfire")


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
            "--python",
            "/usr/local/bin/python",
            "--country",
            "us",
            "--country",
            "uk",
            "--data-dir",
            SIMULATION_BUNDLE_DATA_DIR,
            "--yes",
        ]
    )


# Heavy image with model snapshot for simulation
simulation_image = (
    modal.Image.debian_slim(python_version="3.13")
    .pip_install(
        "uv",
        "fastapi>=0.115.0",
        "tables>=3.10.2",
        "logfire",
    )
    .run_commands(
        bundle_install_command(POLICYENGINE_VERSION),
        secrets=[data_secret, hf_secret],
    )
    .env(VERSION_ENV)
    # TEMPORARY: remove once single-year datasets are published (issue #596).
    # Prebuild single-year datasets into the image so cold containers skip
    # the slow runtime build. One layer per country: independent cache
    # entries and resumable builds. These layers MUST stay before
    # add_local_python_source — that layer is keyed on source file hashes,
    # so anything after it rebuilds on every code change, and these layers
    # take hours. To force a rebuild of a cached layer (e.g. after a data
    # re-release under the same revision), temporarily add force_build=True.
    .run_function(
        prebuild_country_datasets,
        args=("us",),
        secrets=[data_secret, hf_secret],
        cpu=8.0,
        memory=65536,
        timeout=4 * 60 * 60,
    )
    .run_function(
        prebuild_country_datasets,
        args=("uk",),
        secrets=[data_secret, hf_secret],
        cpu=8.0,
        memory=32768,
        timeout=4 * 60 * 60,
    )
    .add_local_python_source(
        "src.modal",
        "policyengine_api_simulation",
        copy=True,
    )
    .run_function(snapshot_models)
)


def configure_logfire(service_name: str = "policyengine-simulation"):
    """Configure Logfire for observability. Call at start of each function."""
    import logfire

    token = os.environ.get("LOGFIRE_TOKEN", "")
    if not token:
        return

    logfire.configure(
        service_name=service_name,
        token=token,
        environment=os.environ.get("LOGFIRE_ENVIRONMENT", "production"),
        console=False,
    )


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
    Logs input params and output result to Logfire for observability.
    """
    import logfire

    from policyengine_api_simulation.simulation_runtime import run_simulation_impl

    configure_logfire()

    # We deliberately avoid sending full ``params`` or ``result`` blobs to
    # Logfire: both can embed signed URLs, reform parameter trees with
    # sensitive policy details, or result payloads large enough to blow the
    # span attribute size budget. The redacted summary keeps correlation
    # traceability via run_id while leaving the heavy payload in memory.
    redacted_params = redact_params_for_logging(params)
    try:
        with logfire.span(
            "run_simulation",
            **redacted_params,
        ):
            return run_simulation_impl(params)
    finally:
        logfire.force_flush()


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
    import logfire

    from src.modal.budget_window_batch import run_budget_window_batch_impl

    configure_logfire()

    redacted_params = redact_params_for_logging(params)
    try:
        with logfire.span(
            "run_budget_window_batch",
            **redacted_params,
        ):
            return run_budget_window_batch_impl(params)
    finally:
        logfire.force_flush()
