"""TEMPORARY: minimum reproduction / pre-warm app for the dataset prebuild.

Remove once single-year datasets are published (see issue #596).

Builds ONLY the image layers up to and including the single-year dataset
prebuild (no local source, no model snapshot), then runs a container from
that image to verify the baked files exist and that the runtime load path
cache-hits. Because the layers are constructed via
``app.build_base_simulation_image()``, they are byte-identical to the real
app's layers, so a successful run leaves them in Modal's workspace image
cache and the next real deploy fast-forwards through them.

Usage (from projects/policyengine-api-simulation):

    uv run modal run --env=staging src/modal/prewarm_app.py

The slow part is the image build (streamed before the function runs); the
verification function itself takes seconds. Interrupting the local client
mid-build does not deploy anything — the app here is ephemeral.
"""

import json

import modal

from src.modal._image_setup import PREBUILD_DATASET_YEARS
from src.modal.app import build_base_simulation_image

app = modal.App("policyengine-simulation-prebuild-prewarm")


@app.function(
    image=build_base_simulation_image(),
    cpu=4.0,
    memory=32768,
    timeout=1800,
    serialized=True,
)
def verify_prebuilt_datasets(years: list[int]) -> dict:
    """Verify baked datasets from inside a container on the built image.

    Serialized so the container never imports this module (the base image
    deliberately lacks the local source packages).
    """
    import os
    import time
    from importlib import import_module
    from pathlib import Path

    os.environ.setdefault("POLICYENGINE_SKIP_COUNTRY_IMPORTS", "1")
    from policyengine.provenance.manifest import (
        dataset_logical_name,
        get_release_manifest,
        resolve_dataset_reference,
    )

    data_folder = Path(
        os.environ.get("POLICYENGINE_DATA_FOLDER", "/opt/policyengine/data")
    )
    default_dataset = get_release_manifest("us").default_dataset
    stem = dataset_logical_name(resolve_dataset_reference("us", default_dataset))

    report: dict[str, str] = {}
    missing = []
    for year in years:
        path = data_folder / f"{stem}_year_{year}.h5"
        if path.exists():
            report[path.name] = f"{path.stat().st_size / 1e6:.1f} MB"
        else:
            missing.append(path.name)
    if missing:
        raise RuntimeError(f"Baked datasets missing from image: {missing}")

    # Time the exact runtime load path: a cache hit loads the baked h5 in
    # seconds; minutes would mean ensure_datasets fell back to a rebuild
    # (i.e. the stems do not match what the runtime resolves).
    start = time.monotonic()
    country_module = import_module("policyengine.tax_benefit_models.us")
    country_module.ensure_datasets(
        datasets=[default_dataset],
        years=[years[0]],
        data_folder=str(data_folder),
    )
    report["ensure_datasets_load_seconds"] = f"{time.monotonic() - start:.1f}"
    return report


@app.local_entrypoint()
def main():
    report = verify_prebuilt_datasets.remote(years=PREBUILD_DATASET_YEARS)
    print(json.dumps(report, indent=2))
