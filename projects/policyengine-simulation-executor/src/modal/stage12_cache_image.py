"""Self-contained Stage 12 cache fetch used during Modal image construction.

It runs as an image-build step before the package source is added to the
image, so it imports only the standard library at module level and nothing
from the package. The deploying machine passes it the manifest and the
contents the cache must hold (``expected_cache_contents``: the years and the
year-file and baseline counts), and each manifest entry names the prebuilt
folder its file lands in.
"""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.cloud.storage import Client


_PREBUILT_DIRECTORY = "prebuilt"
# A prebuilt folder names at least the directory, the country and the dataset.
_MINIMUM_FOLDER_PARTS = 3


def fetch_stage12_cache(
    bucket: str,
    manifest: dict | None,
    expected_contents: dict | None,
    *,
    client: Client | None = None,
) -> None:
    """Download and verify every manifest-listed cache file into the image.

    ``expected_contents`` holds the years and the ``dataset`` and ``baseline``
    counts the manifest must list.
    """
    artifacts, canonical, country = _validated_manifest(
        bucket, manifest, expected_contents
    )
    if client is None:
        client = _storage_client()
    data_dir = Path(os.environ["POLICYENGINE_DATA_FOLDER"])
    destinations = _artifact_destinations(artifacts, data_dir, country=country)
    data_dir.mkdir(parents=True, exist_ok=True)
    for artifact, destination in zip(artifacts, destinations, strict=True):
        _fetch_artifact(artifact, client=client, bucket=bucket, destination=destination)
    (data_dir / ".stage12-cache-manifest.json").write_bytes(canonical)


def _validated_manifest(
    bucket: str, manifest: dict | None, expected_contents: dict | None
) -> tuple[list, bytes, str]:
    """Return the manifest's artifacts, canonical bytes and country once verified.

    The manifest must name the installed bundle, the expected years and
    artifact counts, and match the deployment's digest.
    """
    if not bucket or not manifest:
        msg = "Stage 12 cache deployment has no bucket or manifest"
        raise RuntimeError(msg)
    years, counts = _expected_years_and_counts(expected_contents)
    _require_installed_identity(manifest, years)
    artifacts = _expected_artifacts(manifest, counts)
    canonical = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    manifest_digest = sha256(canonical).hexdigest()
    if manifest_digest != os.getenv("STAGE12_CACHE_MANIFEST_DIGEST"):
        msg = "Stage 12 cache manifest digest differs from deployment"
        raise RuntimeError(msg)
    return artifacts, canonical, str(manifest.get("country"))


def _expected_years_and_counts(
    expected_contents: dict | None,
) -> tuple[list, dict]:
    """Return the years and artifact counts the deployment expects.

    Raises ``RuntimeError`` when the deploying machine passed none, or ones
    of another shape.
    """
    years = (expected_contents or {}).get("years")
    counts = (expected_contents or {}).get("artifact_counts")
    if (
        not isinstance(years, list)
        or not years
        or not all(isinstance(year, int) for year in years)
        or not isinstance(counts, dict)
        or set(counts) != {"dataset", "baseline"}
        or not all(isinstance(count, int) for count in counts.values())
    ):
        msg = "Stage 12 cache deployment has no expected contents"
        raise RuntimeError(msg)
    return years, counts


def _require_installed_identity(manifest: dict, years: list) -> None:
    """Require the manifest's schema, installed bundle and precomputed years."""
    if manifest.get("schema") != "stage12-mf1":
        msg = "Stage 12 cache manifest schema is unsupported"
        raise RuntimeError(msg)
    bundle = manifest.get("bundle")
    if not isinstance(bundle, dict) or bundle.get(
        "bundle_manifest_sha256"
    ) != os.getenv("STAGE12_BUNDLE_MANIFEST_SHA256"):
        msg = "Stage 12 cache manifest names another installed bundle"
        raise RuntimeError(msg)
    if manifest.get("years") != years:
        msg = "Stage 12 cache manifest has an unexpected year set"
        raise RuntimeError(msg)


def _expected_artifacts(manifest: dict, counts: dict) -> list:
    """Return the manifest's artifact list once its contents are as expected.

    It must have the expected numbers of datasets and baselines, no artifact
    of another kind, and either no state datasets or the same states' files
    for every year.
    """
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        msg = "Stage 12 cache manifest has no artifact list"
        raise TypeError(msg)
    entries = [item for item in artifacts if isinstance(item, dict)]
    type_counts = {
        kind: sum(item.get("type") == kind for item in entries)
        for kind in ("dataset", "baseline")
    }
    if type_counts != counts:
        msg = "Stage 12 cache manifest has an unexpected artifact count"
        raise RuntimeError(msg)
    if any(
        item.get("type") not in {"dataset", "baseline", "state_dataset"}
        for item in entries
    ):
        msg = "Stage 12 cache manifest has an unexpected artifact type"
        raise RuntimeError(msg)
    states_by_year = {year: set() for year in manifest["years"]}
    for item in entries:
        if item.get("type") == "state_dataset":
            states_by_year.setdefault(item.get("year"), set()).add(
                item.get("state_code")
            )
    if len({frozenset(states) for states in states_by_year.values()}) > 1:
        msg = "Stage 12 cache manifest's state datasets differ between years"
        raise RuntimeError(msg)
    return artifacts


def _relative_destination(artifact: dict, *, country: str) -> PurePosixPath | None:
    """Return where a listed file lands, relative to the data folder, or None.

    Every file lands in its ``folder``: a relative path without ``.`` or
    ``..`` parts under ``prebuilt/<country>/``, naming at least a dataset, so
    prebuilt files never land beside the source datasets the bundle install
    writes into the data folder. None means the entry names an unsafe place.
    """
    filename = artifact.get("filename")
    if not isinstance(filename, str) or PurePosixPath(filename).name != filename:
        return None
    if "\\" in filename or filename in {"", ".", ".."}:
        return None
    folder = artifact.get("folder")
    if not isinstance(folder, str) or not folder or "\\" in folder:
        return None
    parts = PurePosixPath(folder).parts
    if (
        PurePosixPath(folder).is_absolute()
        or any(part in {".", ".."} for part in parts)
        or "/".join(parts) != folder
        or len(parts) < _MINIMUM_FOLDER_PARTS
        or parts[:2] != (_PREBUILT_DIRECTORY, country)
    ):
        return None
    return PurePosixPath(folder, filename)


def _artifact_destinations(
    artifacts: list, data_dir: Path, *, country: str
) -> list[Path]:
    """Check every listed file's metadata and return where each one lands.

    Runs before anything downloads, so a bad entry leaves no file behind.
    """
    destinations = []
    seen: set[PurePosixPath] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            msg = "Stage 12 cache artifact entry is invalid"
            raise TypeError(msg)
        relative = _relative_destination(artifact, country=country)
        if (
            relative is None
            or relative in seen
            or not isinstance(artifact.get("path"), str)
            or not isinstance(artifact.get("content_sha256"), str)
            or not isinstance(artifact.get("size_bytes"), int)
        ):
            msg = "Stage 12 cache artifact metadata is invalid"
            raise RuntimeError(msg)
        seen.add(relative)
        destinations.append(data_dir.joinpath(*relative.parts))
    return destinations


def _storage_client() -> Client:
    """Build a storage client from the deployment's service-account credentials.

    ``GOOGLE_APPLICATION_CREDENTIALS_JSON`` holds the credentials object, or
    that object encoded as a JSON string; without it, the client uses the
    environment's default credentials.
    """
    credentials = {}
    raw_credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if raw_credentials:
        decoded = json.loads(raw_credentials)
        info = json.loads(decoded) if isinstance(decoded, str) else decoded
        from google.oauth2 import service_account

        credentials = {
            "credentials": service_account.Credentials.from_service_account_info(info),
            "project": info.get("project_id"),
        }
    from google.cloud import storage

    return storage.Client(**credentials)


def _fetch_artifact(
    artifact: dict,
    *,
    client: Client,
    bucket: str,
    destination: Path,
) -> None:
    """Download one checked file to ``destination`` and verify its size and digest.

    The download goes to a partial file beside its destination and is moved
    into place only once verified, so a failed download leaves nothing.
    """
    expected_digest = artifact["content_sha256"]
    expected_size = artifact["size_bytes"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    try:
        client.bucket(bucket).blob(artifact["path"]).download_to_filename(str(partial))
        digest = sha256()
        with partial.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        if partial.stat().st_size != expected_size:
            msg = "Stage 12 cache artifact size differs"
            raise RuntimeError(msg)
        if digest.hexdigest() != expected_digest:
            msg = "Stage 12 cache artifact digest differs"
            raise RuntimeError(msg)
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
