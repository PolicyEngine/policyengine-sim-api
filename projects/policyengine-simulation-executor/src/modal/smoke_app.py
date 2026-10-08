"""Pre-merge image smoke: validate and import the executor runtime image.

The image here is the deployed image's layer prefix (frozen uv sync,
dataset-only PolicyEngine bundle install, version env) plus the source mounts.
It deliberately excludes the artifact-fetch and model-snapshot layers, which
add no Python packages. Because layers are content-addressed and built through
the shared ``build_runtime_simulation_image()``, a warm cache makes this run
take seconds; after a relock it pays only the bundle install.

Before importing worker code, the smoke uses PolicyEngine's bundle status
check. That compares all selected package versions with the bundle manifest,
reads the dataset-install receipt, and hashes both installed country datasets.

A separate 64-GiB function prepares the certified US regional dataset from
scratch and calculates Utah. This is real data/model coverage; package imports
and national-dataset hashes alone cannot establish ACS compatibility.

Runs the imports the deployed workers perform lazily at request time —
``run_simulation_impl``, the budget-window batch, and both shared libraries.

Usage:
    uv run modal run --env=staging src/modal/smoke_app.py
"""

from pathlib import Path

import modal
from policyengine_simulation_executor.release_bundle import (
    resolve_local_bundle_dataset_path,
)
from policyengine_simulation_executor.uk_local_authority_metadata import (
    detect_uk_local_authority_metadata_from_hdf,
)
from src.modal.app import build_runtime_simulation_image
from src.modal.static_runtime_files import add_static_runtime_files

app = modal.App("policyengine-simulation-executor-smoke")

smoke_image = add_static_runtime_files(
    build_runtime_simulation_image().add_local_python_source(
        "src.modal",
        "policyengine_simulation_executor",
        "policyengine_simulation_observability",
        "policyengine_simulation_contract",
        copy=True,
    ),
    uv_project_dir=(
        str(Path(__file__).resolve().parents[2]) if modal.is_local() else "."
    ),
)

_EXPECTED_RUNTIME_PACKAGES = frozenset(
    {
        "policyengine",
        "policyengine-core",
        "policyengine-uk",
        "policyengine-us",
        "spm-calculator",
    }
)
_EXPECTED_COUNTRIES = frozenset({"us", "uk"})


def _summarize_bundle_status(status: dict) -> dict:
    """Reject an incomplete or mismatched runtime bundle and summarize it."""
    import json

    package_names = {check.get("package") for check in status.get("packages", [])}
    dataset_countries = {check.get("country") for check in status.get("datasets", [])}
    receipt = status.get("receipt")
    receipt_countries = (
        set(receipt.get("countries", [])) if isinstance(receipt, dict) else set()
    )
    complete = (
        status.get("matched") is True
        and package_names == _EXPECTED_RUNTIME_PACKAGES
        and dataset_countries == _EXPECTED_COUNTRIES
        and receipt_countries == _EXPECTED_COUNTRIES
    )
    if not complete:
        raise RuntimeError(
            "PolicyEngine runtime bundle validation failed:\n"
            + json.dumps(status, indent=2, sort_keys=True)
        )
    return {
        "bundle_version": status["bundle_version"],
        "packages": {
            check["package"]: check["installed_version"] for check in status["packages"]
        },
        "datasets": {
            check["country"]: {
                "dataset": check["dataset"],
                "version": check["expected_version"],
                "sha256": check["expected_sha256"],
            }
            for check in status["datasets"]
        },
    }


def _validate_installed_uk_local_authority_dataset() -> str:
    """Require the installed UK dataset to match one packaged boundary version."""

    dataset_path = resolve_local_bundle_dataset_path("uk", None)
    if dataset_path is None:
        raise RuntimeError("installed certified UK dataset is unavailable")
    metadata = detect_uk_local_authority_metadata_from_hdf(dataset_path)
    return metadata.boundary_version.value


@app.function(image=smoke_image, timeout=600, memory=8192)
def smoke_import_executor() -> dict:
    import importlib
    import os
    import pkgutil
    from pathlib import Path

    from policyengine.bundle import inspect_bundle_status

    bundle = _summarize_bundle_status(
        inspect_bundle_status(
            os.environ["POLICYENGINE_VERSION"],
            countries=["us", "uk"],
            data_dir=Path(os.environ["POLICYENGINE_DATA_FOLDER"]),
        )
    )
    bundle["uk_local_authority_boundary_version"] = (
        _validate_installed_uk_local_authority_dataset()
    )

    # Module-level surface of the deployed app (versions resolve from the
    # baked env layer).
    importlib.import_module("src.modal.app")

    # The lazy request-time imports of the worker functions. run_simulation
    # imports the segmented-national dispatch (and its pandas/microdf/
    # policyengine.core chain) on EVERY request, so a break there crashes
    # all requests at import time — exactly the #602 failure class this
    # smoke exists to catch.
    import policyengine_simulation_contract
    import policyengine_simulation_observability

    from policyengine_simulation_executor.simulation_runtime import (  # noqa: F401
        run_simulation_impl,
    )
    from src.modal.budget_window_batch import (  # noqa: F401
        run_budget_window_batch_impl,
    )
    from src.modal.segmented_national import (  # noqa: F401
        dispatch_run_simulation,
    )

    imported = []
    for package in (
        policyengine_simulation_contract,
        policyengine_simulation_observability,
    ):
        for module in pkgutil.walk_packages(
            package.__path__, prefix=f"{package.__name__}."
        ):
            importlib.import_module(module.name)
            imported.append(module.name)

    return {"modules_imported": len(imported), "bundle": bundle}


@app.local_entrypoint()
def main():
    report = smoke_import_executor.remote()
    print(report)
    regional_report = smoke_us_regional_dataset.remote()
    print(regional_report)
    print("US regional dataset preparation and calculation OK")
    print("executor image smoke OK")


@app.function(image=smoke_image, timeout=1800, cpu=8.0, memory=65536, max_containers=1)
def smoke_us_regional_dataset() -> dict[str, str | int]:
    """Ephemeral PR check; do not publish outputs or change deployed workers."""
    from src.modal.us_regional_dataset_check import check_certified_us_regional_dataset

    report = check_certified_us_regional_dataset()
    return report.model_dump()
