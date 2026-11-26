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
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any

    from google.cloud import storage


# The manifest schema this step reads (precompute_models.MANIFEST_SCHEMA;
# this module cannot import it).
_MANIFEST_SCHEMA = "mf2"
_PREBUILT_DIRECTORY = "prebuilt"
_HASH_CHUNK_BYTES = 1024 * 1024


def fetch_artifacts(
    bucket: str,
    manifest: "Mapping[str, Any] | None",
    *,
    client: "storage.Client | None" = None,
) -> None:
    """Download every artifact one country's manifest lists into its prebuilt folder.

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
        client.bucket(bucket),
        manifest["artifacts"],
        data_folder,
        country=str(manifest["country"]),
    )


def _require_manifest_matches_image(manifest: "Mapping[str, Any]") -> None:
    """Freshness check: refuse artifacts computed for another version-set.

    The manifest must have the schema this step reads, and the image's
    policyengine, policyengine-core and country model versions must equal the
    manifest receipt's, and so must the installed data version when the
    bundle receipt records one.
    """
    if manifest.get("schema") != _MANIFEST_SCHEMA:
        msg = (
            f"The artifact manifest has schema {manifest.get('schema')!r}; this "
            f"image reads {_MANIFEST_SCHEMA!r}. Re-run the precompute for the "
            "current code."
        )
        raise RuntimeError(msg)
    country = str(manifest["country"])
    receipt = manifest["receipt"]
    expected_env = {
        "POLICYENGINE_VERSION": receipt["policyengine_version"],
        "POLICYENGINE_CORE_VERSION": receipt["policyengine_core_version"],
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


def _artifact_target(
    data_folder: "Path", artifact: "Mapping[str, Any]", *, country: str
) -> "Path":
    """Return where an artifact lands: its ``folder`` under the data folder.

    Every folder sits under ``prebuilt/<country>/``, so prebuilt files never
    land beside the source datasets the bundle install writes into the data
    folder's root. Anything that could leave that folder, or name a different
    file than intended, is refused.
    """
    filename = artifact.get("filename")
    if (
        not isinstance(filename, str)
        or filename in {"", ".", ".."}
        or "/" in filename
        or "\\" in filename
    ):
        msg = f"Artifact {artifact.get('path')!r} has an unsafe filename {filename!r}"
        raise RuntimeError(msg)
    folder = artifact.get("folder")
    if not isinstance(folder, str) or not folder or "\\" in folder:
        msg = f"Artifact {artifact.get('path')!r} has an unsafe folder {folder!r}"
        raise RuntimeError(msg)
    parts = PurePosixPath(folder).parts
    if (
        len(parts) < 3  # noqa: PLR2004  # prebuilt, the country, and at least the dataset
        or PurePosixPath(folder).is_absolute()
        or any(part in {".", ".."} for part in parts)
        or "/".join(parts) != folder
        or parts[:2] != (_PREBUILT_DIRECTORY, country.lower())
    ):
        msg = f"Artifact {artifact.get('path')!r} has an unsafe folder {folder!r}"
        raise RuntimeError(msg)
    return data_folder.joinpath(*parts, filename)


def _file_sha256(path: "Path") -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _download_checked(
    bucket_handle: "storage.Bucket", artifact: "Mapping[str, Any]", target: "Path"
) -> None:
    """Download one artifact beside ``target`` and move it into place once checked.

    The downloaded file's size and SHA-256 must equal the manifest's, so a
    failed or altered download leaves nothing at ``target``.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    try:
        bucket_handle.blob(artifact["path"]).download_to_filename(str(partial))
        if not partial.exists():
            msg = f"Downloading {artifact['path']} wrote no file for {target.name}"
            raise RuntimeError(msg)
        if partial.stat().st_size != artifact["size_bytes"]:
            msg = (
                f"{target.name} from {artifact['path']} has {partial.stat().st_size} "
                f"bytes, not the manifest's {artifact['size_bytes']}"
            )
            raise RuntimeError(msg)
        if _file_sha256(partial) != artifact["sha256"]:
            msg = (
                f"{target.name} from {artifact['path']} does not have the "
                "manifest's SHA-256"
            )
            raise RuntimeError(msg)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def _download_manifest_artifacts(
    bucket_handle: "storage.Bucket",
    artifacts: "Sequence[Mapping[str, Any]]",
    data_folder: "Path",
    *,
    country: str,
) -> None:
    """Download every artifact to its prebuilt folder, failing on any gap.

    Every target and store object is checked before anything downloads: a
    target listed twice, or already in the image (another country's step
    must never replace the first's files), stops the build. Every expected
    file is checked after.
    """
    logger = logging.getLogger(__name__)
    targets = [
        _artifact_target(data_folder, artifact, country=country)
        for artifact in artifacts
    ]
    repeated = sorted({str(target) for target in targets if targets.count(target) > 1})
    if repeated:
        msg = f"The manifest lists these files more than once: {repeated}"
        raise RuntimeError(msg)
    existing = [str(target) for target in targets if target.exists()]
    if existing:
        msg = f"These files are already in the image: {existing}"
        raise RuntimeError(msg)
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

    for artifact, target in zip(artifacts, targets, strict=True):
        _download_checked(bucket_handle, artifact, target)
        logger.info("Fetched %s (%.1f MB)", target, target.stat().st_size / 1e6)

    absent = [str(target) for target in targets if not target.exists()]
    if absent:
        msg = f"Artifact fetch did not produce expected files: {absent}"
        raise RuntimeError(msg)


def check_country_models_build() -> None:
    """Fail the image build unless both countries' tax-benefit systems build.

    The systems are built and discarded: an image layer keeps files, not a
    process's memory, so nothing built here reaches a worker. The workers load
    the models in their memory-snapshot hook instead
    (``src/modal/country_model_preload.py``).
    """
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    logger.info("Checking that the US tax-benefit system builds...")
    from policyengine_us import (
        CountryTaxBenefitSystem as USSystem,
    )

    USSystem()

    logger.info("Checking that the UK tax-benefit system builds...")
    from policyengine_uk import (
        CountryTaxBenefitSystem as UKSystem,
    )

    UKSystem()

    logger.info("Both countries' tax-benefit systems build")
