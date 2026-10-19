"""Self-contained Stage 12 cache fetch used during Modal image construction."""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from google.oauth2 import service_account

if TYPE_CHECKING:
    from google.cloud.storage import Client


def fetch_stage12_cache(
    bucket: str, manifest: dict | None, *, client: Client | None = None
) -> None:
    """Download and verify every manifest-listed cache file into the image."""
    artifacts, canonical = _validated_manifest(bucket, manifest)
    if client is None:
        client = _storage_client()
    data_dir = Path(os.environ["POLICYENGINE_DATA_FOLDER"])
    data_dir.mkdir(parents=True, exist_ok=True)
    filenames: set[str] = set()
    for artifact in artifacts:
        _fetch_artifact(
            artifact,
            client=client,
            bucket=bucket,
            data_dir=data_dir,
            filenames=filenames,
        )
    (data_dir / ".stage12-cache-manifest.json").write_bytes(canonical)


def _validated_manifest(bucket: str, manifest: dict | None) -> tuple[list, bytes]:
    """Return the manifest's artifacts and canonical bytes once it is verified.

    The manifest must name the installed bundle, the expected years and
    artifact counts, and match the deployment's digest.
    """
    if not bucket or not manifest:
        msg = "Stage 12 cache deployment has no bucket or manifest"
        raise RuntimeError(msg)
    _require_installed_identity(manifest)
    artifacts = _expected_artifacts(manifest)
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
    return artifacts, canonical


def _require_installed_identity(manifest: dict) -> None:
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
    if manifest.get("years") != [2026, 2027, 2025]:
        msg = "Stage 12 cache manifest has an unexpected year set"
        raise RuntimeError(msg)


def _expected_artifacts(manifest: dict) -> list:
    """Return the manifest's artifact list once it has 3 datasets and 60 baselines."""
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        msg = "Stage 12 cache manifest has no artifact list"
        raise TypeError(msg)
    type_counts = {
        kind: sum(
            item.get("type") == kind for item in artifacts if isinstance(item, dict)
        )
        for kind in ("dataset", "baseline")
    }
    if type_counts != {"dataset": 3, "baseline": 60}:
        msg = "Stage 12 cache manifest has an unexpected artifact count"
        raise RuntimeError(msg)
    return artifacts


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
        credentials = {
            "credentials": service_account.Credentials.from_service_account_info(info),
            "project": info.get("project_id"),
        }
    from google.cloud import storage

    return storage.Client(**credentials)


def _fetch_artifact(
    artifact: object,
    *,
    client: Client,
    bucket: str,
    data_dir: Path,
    filenames: set[str],
) -> None:
    """Download one listed file into ``data_dir`` and verify its size and digest.

    The download goes to a partial file beside its destination and is moved
    into place only once verified, so a failed download leaves nothing.
    """
    if not isinstance(artifact, dict):
        msg = "Stage 12 cache artifact entry is invalid"
        raise TypeError(msg)
    filename = artifact.get("filename")
    path = artifact.get("path")
    expected_digest = artifact.get("content_sha256")
    expected_size = artifact.get("size_bytes")
    if (
        not isinstance(filename, str)
        or Path(filename).name != filename
        or filename in filenames
        or not isinstance(path, str)
        or not isinstance(expected_digest, str)
        or not isinstance(expected_size, int)
    ):
        msg = "Stage 12 cache artifact metadata is invalid"
        raise RuntimeError(msg)
    filenames.add(filename)
    destination = data_dir / filename
    partial = destination.with_name(filename + ".partial")
    try:
        client.bucket(bucket).blob(path).download_to_filename(str(partial))
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
