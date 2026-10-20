"""Standalone image setup functions.

These functions are executed during Modal image build and must not
import any other modules from this package to avoid dependency issues.
The artifact fetch additionally runs BEFORE add_local_python_source,
so policyengine_simulation_executor is not importable there at all.
"""

# Only standard-library modules are imported at module level. Modal imports
# this module from its mounted source in the image-build step, before the
# executor package is added to the image, and src/modal/app.py imports it
# wherever the app loads; any other package loads only inside the function
# that needs it. Annotation-only imports sit under TYPE_CHECKING, so the
# annotations that use them are quoted.
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any

    from google.cloud import storage


def fetch_artifacts(
    bucket: str,
    manifest: "Mapping[str, Any] | None",
    *,
    client: "storage.Client | None" = None,
) -> None:
    """Download every manifest-listed artifact into the image data folder.

    Runs as an image-build layer between ``.env(VERSION_ENV)`` and
    ``add_local_python_source``, so it must stay self-contained (no
    imports from this package; only the standard library at module
    level, everything else in the function body). The manifest
    rides in the layer args: it is content-addressed, so the layer cache
    busts exactly when the deployed artifact set changes.

    ``manifest`` is None when the deploying machine resolved no manifest
    (POLICYENGINE_MANIFEST_DIGEST unset) — importing the app module must
    stay safe for the precompute and smoke apps, so that case fails here,
    at build time, instead of at import time.

    ``client`` is a test seam; real builds construct a storage client from
    the layer's GCP secret (or ambient ADC for local drill runs).
    """
    logging.basicConfig(level=logging.INFO)

    if not manifest:
        msg = (
            "No artifact manifest was resolved on the deploying machine. "
            "Set POLICYENGINE_MANIFEST_DIGEST (from the precompute run's "
            "MANIFEST_DIGEST= output line) and POLICYENGINE_ARTIFACT_BUCKET, "
            "with GCP credentials available, then redeploy."
        )
        raise RuntimeError(msg)

    _require_manifest_matches_image(manifest)
    if client is None:
        client = _storage_client_from_environment()

    data_folder = Path(
        os.environ.get("POLICYENGINE_DATA_FOLDER", "/opt/policyengine/data")
    )
    data_folder.mkdir(parents=True, exist_ok=True)
    _download_manifest_artifacts(
        client.bucket(bucket), manifest["artifacts"], data_folder
    )


def _require_manifest_matches_image(manifest: "Mapping[str, Any]") -> None:
    """Freshness gate: refuse artifacts computed for another version-set.

    The image's policyengine and country model versions must equal the
    manifest receipt's, and so must the installed data version when the
    bundle receipt records one.
    """
    country = str(manifest["country"])
    receipt = manifest["receipt"]
    expected_env = {
        "POLICYENGINE_VERSION": receipt["policyengine_version"],
        f"POLICYENGINE_{country.upper()}_VERSION": receipt["model_version"],
    }
    for env_var, manifest_value in expected_env.items():
        image_value = os.environ.get(env_var)
        if image_value != manifest_value:
            msg = (
                f"Stale artifact manifest: the image has "
                f"{env_var}={image_value!r} but the manifest was computed "
                f"for {manifest_value!r}. Re-run the precompute for the "
                "current version-set."
            )
            raise RuntimeError(msg)

    installed_data_version = _installed_data_version(country)
    if installed_data_version is None:
        # Lenient by design: local drill runs have no bundle receipt file.
        logging.getLogger(__name__).warning(
            "No installed bundle receipt entry for %s; "
            "skipping the data-version freshness check",
            country,
        )
    elif installed_data_version != receipt["data_version"]:
        msg = (
            f"Stale artifact manifest: installed {country} data version is "
            f"{installed_data_version!r} but the manifest was computed for "
            f"{receipt['data_version']!r}. Re-run the precompute for the "
            "current version-set."
        )
        raise RuntimeError(msg)


def _installed_data_version(country: str) -> object:
    """Return the bundle receipt's data version for ``country``, or None.

    None also covers a missing or unreadable receipt; the last matching
    receipt entry wins.
    """
    receipt_path = os.environ.get("POLICYENGINE_BUNDLE_RECEIPT")
    if not receipt_path or not Path(receipt_path).exists():
        return None
    try:
        installed = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    installed_data_version = None
    if isinstance(installed, dict):
        for dataset in installed.get("datasets", []):
            if isinstance(dataset, dict) and dataset.get("country") == country:
                installed_data_version = dataset.get("version")
    return installed_data_version


def _storage_client_from_environment() -> "storage.Client":
    """Build a storage client from the layer's GCP secret, else ambient ADC."""
    creds_kwargs = {}
    blob = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if blob:
        creds_kwargs = _service_account_credentials(blob)
    # No blob: ambient ADC (the local fault-drill path).
    from google.cloud import (
        storage,
    )

    return storage.Client(**creds_kwargs)


def _service_account_credentials(blob: str) -> "dict[str, Any]":
    """Return storage client arguments for the service account in ``blob``.

    Credentials are built in memory, never written to disk: this layer's
    container filesystem is committed into the image, so a credentials file
    would ship inside it.
    """
    # The secret payload is sometimes double-encoded (escaped interior
    # quotes, with or without outer quotes) — same unwrap as the runtime's
    # _normalize_credentials_blob.
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        if blob.lstrip().startswith('"') or '\\"' in blob:
            parsed = json.loads(f'"{blob}"')
        else:
            raise
    info = json.loads(parsed) if isinstance(parsed, str) else parsed

    from google.oauth2 import (
        service_account,
    )

    return {
        "credentials": service_account.Credentials.from_service_account_info(info),
        "project": info.get("project_id"),
    }


def _download_manifest_artifacts(
    bucket_handle: "storage.Bucket",
    artifacts: "Sequence[Mapping[str, Any]]",
    data_folder: "Path",
) -> None:
    """Download every artifact under its runtime filename, failing on any gap.

    Every store object is checked before anything downloads, and every
    expected file is checked after.
    """
    logger = logging.getLogger(__name__)
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
        msg = f"Artifact fetch did not produce expected files: {absent}"
        raise RuntimeError(msg)


def snapshot_models() -> None:
    """Pre-load models at image build time for fast cold starts."""
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    logger.info("Pre-loading US tax-benefit system...")
    from policyengine_us import (
        CountryTaxBenefitSystem as USSystem,
    )

    USSystem()

    logger.info("Pre-loading UK tax-benefit system...")
    from policyengine_uk import (
        CountryTaxBenefitSystem as UKSystem,
    )

    UKSystem()

    logger.info("Models pre-loaded into image snapshot")
