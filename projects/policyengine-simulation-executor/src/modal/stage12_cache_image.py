"""Self-contained Stage 12 cache fetch used during Modal image construction."""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

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
    destinations = _artifact_destinations(artifacts, data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    for artifact, destination in zip(artifacts, destinations, strict=True):
        _fetch_artifact(artifact, client=client, bucket=bucket, destination=destination)
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
    """Return the manifest's artifact list once its contents are as expected.

    It must have 3 datasets and 60 baselines, no artifact of another kind, and
    either no state datasets or the same states' files for every year.
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
    if type_counts != {"dataset": 3, "baseline": 60}:
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


def _relative_destination(artifact: dict) -> PurePosixPath | None:
    """Return where a listed file lands, relative to the data folder, or None.

    Dataset and baseline files land directly in the data folder; a state
    dataset lands in its ``folder``, a relative path without ``.`` or ``..``
    parts. None means the entry names an unsafe place.
    """
    filename = artifact.get("filename")
    if not isinstance(filename, str) or PurePosixPath(filename).name != filename:
        return None
    if "\\" in filename or filename in {"", ".", ".."}:
        return None
    if artifact.get("type") != "state_dataset":
        return PurePosixPath(filename)
    folder = artifact.get("folder")
    if not isinstance(folder, str) or not folder or "\\" in folder:
        return None
    parts = PurePosixPath(folder).parts
    if (
        PurePosixPath(folder).is_absolute()
        or any(part in {".", ".."} for part in parts)
        or "/".join(parts) != folder
    ):
        return None
    return PurePosixPath(folder, filename)


def _artifact_destinations(artifacts: list, data_dir: Path) -> list[Path]:
    """Check every listed file's metadata and return where each one lands.

    Runs before anything downloads, so a bad entry leaves no file behind.
    """
    destinations = []
    seen: set[PurePosixPath] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            msg = "Stage 12 cache artifact entry is invalid"
            raise TypeError(msg)
        relative = _relative_destination(artifact)
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
