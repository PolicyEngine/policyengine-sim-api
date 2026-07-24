"""
Standalone image setup functions.

These functions are executed during Modal image build and must not
import any other modules from this package to avoid dependency issues.
The artifact fetch additionally runs BEFORE add_local_python_source,
so policyengine_simulation_executor is not importable there at all.
"""


def fetch_artifacts(bucket: str, manifest, *, client=None):
    """Download every manifest-listed artifact into the image data folder.

    Runs as an image-build layer between ``.env(VERSION_ENV)`` and
    ``add_local_python_source``, so it must stay self-contained (no
    imports from this package; everything lazy in-body). The manifest
    rides in the layer args: it is content-addressed, so the layer cache
    busts exactly when the deployed artifact set changes.

    ``manifest`` is None when the deploying machine resolved no manifest
    (POLICYENGINE_MANIFEST_DIGEST unset) — importing the app module must
    stay safe for the precompute and smoke apps, so that case fails here,
    at build time, instead of at import time.

    ``client`` is a test seam; real builds construct a storage client from
    the layer's GCP secret (or ambient ADC for local drill runs).
    """
    import json
    import logging
    import os
    from pathlib import Path

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    if not manifest:
        raise RuntimeError(
            "No artifact manifest was resolved on the deploying machine. "
            "Set POLICYENGINE_MANIFEST_DIGEST (from the precompute run's "
            "MANIFEST_DIGEST= output line) and POLICYENGINE_ARTIFACT_BUCKET, "
            "with GCP credentials available, then redeploy."
        )

    # Freshness gate: refuse to bake artifacts computed for a different
    # version-set than the one this image installs.
    country = str(manifest["country"])
    receipt = manifest["receipt"]
    expected_env = {
        "POLICYENGINE_VERSION": receipt["policyengine_version"],
        f"POLICYENGINE_{country.upper()}_VERSION": receipt["model_version"],
    }
    for env_var, manifest_value in expected_env.items():
        image_value = os.environ.get(env_var)
        if image_value != manifest_value:
            raise RuntimeError(
                f"Stale artifact manifest: the image has "
                f"{env_var}={image_value!r} but the manifest was computed "
                f"for {manifest_value!r}. Re-run the precompute for the "
                "current version-set."
            )

    installed_data_version = None
    receipt_path = os.environ.get("POLICYENGINE_BUNDLE_RECEIPT")
    if receipt_path and Path(receipt_path).exists():
        try:
            installed = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            installed = None
        if isinstance(installed, dict):
            for dataset in installed.get("datasets", []):
                if isinstance(dataset, dict) and dataset.get("country") == country:
                    installed_data_version = dataset.get("version")
    if installed_data_version is None:
        # Lenient by design: local drill runs have no bundle receipt file.
        logger.warning(
            "No installed bundle receipt entry for %s; "
            "skipping the data-version freshness check",
            country,
        )
    elif installed_data_version != receipt["data_version"]:
        raise RuntimeError(
            f"Stale artifact manifest: installed {country} data version is "
            f"{installed_data_version!r} but the manifest was computed for "
            f"{receipt['data_version']!r}. Re-run the precompute for the "
            "current version-set."
        )

    if client is None:
        creds_kwargs = {}
        blob = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        if blob:
            # The secret payload is sometimes double-encoded (escaped
            # interior quotes, with or without outer quotes) — same
            # unwrap as the runtime's _normalize_credentials_blob.
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError:
                if blob.lstrip().startswith('"') or '\\"' in blob:
                    parsed = json.loads(f'"{blob}"')
                else:
                    raise
            info = json.loads(parsed) if isinstance(parsed, str) else parsed

            from google.oauth2 import service_account

            # Credentials built in memory, never written to disk: this
            # layer's container filesystem is committed into the image,
            # so a credentials file would ship inside it.
            creds_kwargs = {
                "credentials": service_account.Credentials.from_service_account_info(
                    info
                ),
                "project": info.get("project_id"),
            }
        # No blob: ambient ADC (the local fault-drill path).
        from google.cloud import storage

        client = storage.Client(**creds_kwargs)

    data_folder = Path(
        os.environ.get("POLICYENGINE_DATA_FOLDER", "/opt/policyengine/data")
    )
    data_folder.mkdir(parents=True, exist_ok=True)
    bucket_handle = client.bucket(bucket)
    artifacts = manifest["artifacts"]

    missing = [
        artifact["path"]
        for artifact in artifacts
        if not bucket_handle.blob(artifact["path"]).exists()
    ]
    if missing:
        # Fail the image build loudly rather than shipping an image
        # with a partial artifact set. Heal: re-run the precompute
        # (a deleted object is an ordinary miss to it).
        raise RuntimeError(
            "Artifact store is missing objects the manifest lists: "
            + ", ".join(missing)
        )

    # Unconditional downloads: a same-named stale file must never
    # survive into the image, and the layer starts empty of these
    # files anyway.
    for artifact in artifacts:
        target = data_folder / artifact["filename"]
        bucket_handle.blob(artifact["path"]).download_to_filename(str(target))
        logger.info("Fetched %s (%.1f MB)", target, target.stat().st_size / 1e6)

    absent = [
        str(data_folder / artifact["filename"])
        for artifact in artifacts
        if not (data_folder / artifact["filename"]).exists()
    ]
    if absent:
        raise RuntimeError(f"Artifact fetch did not produce expected files: {absent}")


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
