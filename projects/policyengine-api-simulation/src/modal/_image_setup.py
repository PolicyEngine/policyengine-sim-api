"""
Standalone image setup functions.

These functions are executed during Modal image build and must not
import any other modules from this package to avoid dependency issues.
The dataset prebuild additionally runs BEFORE add_local_python_source,
so policyengine_api_simulation is not importable there at all.
"""

# TEMPORARY: remove once single-year datasets are published (see issue #596).
# These years are baked into the image; uncovered years still build at runtime.
# NOTE: this constant is a referenced global of prebuild_country_datasets, so
# changing it (like editing the function body, even comments) invalidates the
# cached multi-hour image layers.
PREBUILD_DATASET_YEARS = [2025, 2026, 2027]


def prebuild_country_datasets(country: str):
    """TEMPORARY: bake single-year national datasets into the image layer.

    Remove once single-year datasets are published (see issue #596).

    policyengine.py's ensure_datasets() rebuilds missing
    ``{stem}_year_{year}.h5`` files at runtime with a full Microsimulation
    pass, which dominates cold-container latency. Building them here bakes
    the files into the image at POLICYENGINE_DATA_FOLDER so the runtime
    existence check hits immediately.
    """
    import logging
    import os
    from importlib import import_module
    from pathlib import Path

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    os.environ.setdefault("POLICYENGINE_SKIP_COUNTRY_IMPORTS", "1")

    from policyengine.provenance.manifest import (
        dataset_logical_name,
        get_release_manifest,
        resolve_dataset_reference,
    )

    data_folder = os.environ.get("POLICYENGINE_DATA_FOLDER", "/opt/policyengine/data")
    # Pass the dataset name explicitly: the UK ensure_datasets has no
    # datasets=None fallback, and the explicit name mirrors the runtime
    # call shape in simulation_runtime._load_dataset.
    default_dataset = get_release_manifest(country).default_dataset
    stem = dataset_logical_name(resolve_dataset_reference(country, default_dataset))
    targets = {
        year: Path(data_folder) / f"{stem}_year_{year}.h5"
        for year in PREBUILD_DATASET_YEARS
    }
    missing_years = [year for year, path in targets.items() if not path.exists()]
    if not missing_years:
        logger.info("All %s single-year datasets already present", country)
        return

    logger.info(
        "Prebuilding %s dataset %s for years %s into %s",
        country,
        default_dataset,
        missing_years,
        data_folder,
    )
    country_module = import_module(f"policyengine.tax_benefit_models.{country}")
    country_module.ensure_datasets(
        datasets=[default_dataset],
        years=missing_years,
        data_folder=data_folder,
    )

    still_missing = [str(path) for path in targets.values() if not path.exists()]
    if still_missing:
        # Fail the image build loudly rather than shipping an image that
        # silently falls back to per-request dataset builds.
        raise RuntimeError(
            f"Prebuild did not produce expected dataset files: {still_missing}"
        )
    for year, path in targets.items():
        logger.info("Prebuilt %s (%.1f MB)", path, path.stat().st_size / 1e6)


def snapshot_models():
    """Pre-load models at image build time for fast cold starts."""
    import logging

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    logger.info("Pre-loading US tax-benefit system...")
    from policyengine_us import CountryTaxBenefitSystem as USSystem

    USSystem()

    logger.info("Pre-loading UK tax-benefit system...")
    from policyengine_uk import CountryTaxBenefitSystem as UKSystem

    UKSystem()

    logger.info("Models pre-loaded into image snapshot")
