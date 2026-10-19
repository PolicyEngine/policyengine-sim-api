"""Dedicated write-once object store for Stage 12 cache files."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.api_core.exceptions import NotFound, PreconditionFailed

from policyengine_simulation_executor.simulation_runtime import (
    setup_gcp_credentials,
)

from .keys import canonical_digest
from .models import CacheObject

if TYPE_CHECKING:
    from google.cloud import storage

CACHE_BUCKET_ENV = "STAGE12_CACHE_BUCKET"


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_cache_bucket(explicit: str | None = None) -> str:
    """Return ``explicit``, or else the bucket named by ``STAGE12_CACHE_BUCKET``.

    Raises ``RuntimeError`` when neither names a bucket.
    """
    bucket = explicit or os.environ.get(CACHE_BUCKET_ENV)
    if not bucket:
        msg = f"Stage 12 cache bucket is not configured; set {CACHE_BUCKET_ENV}"
        raise RuntimeError(msg)
    return bucket


def _default_client() -> storage.Client:
    from google.cloud import storage

    with setup_gcp_credentials():
        return storage.Client()


class Stage12CacheStore:
    """Write-once access to the Stage 12 cache bucket.

    Uploads only create objects, so a stored path is never replaced. Each
    object records its SHA-256 digest and size in its metadata.
    """

    def __init__(
        self, bucket_name: str | None = None, *, client: storage.Client | None = None
    ) -> None:
        """Use ``bucket_name``, or the configured bucket, and an optional client."""
        self.bucket_name = resolve_cache_bucket(bucket_name)
        self._client = client

    @property
    def client(self) -> storage.Client:
        """The storage client, created with the cache credentials on first use."""
        if self._client is None:
            self._client = _default_client()
        return self._client

    def _blob(self, path: str) -> storage.Blob:
        return self.client.bucket(self.bucket_name).blob(path)

    def exists(self, path: str) -> bool:
        """Return whether the bucket holds an object at ``path``."""
        return bool(self._blob(path).exists())

    def upload_file(
        self, path: str, local_path: str | Path
    ) -> tuple[bool, CacheObject]:
        """Upload a local file to ``path`` unless an object already exists there.

        Returns whether this call uploaded it and the stored object. Raises
        ``RuntimeError`` when the stored object's digest or size differs from
        the file's.
        """
        source = Path(local_path)
        payload_digest = _file_sha256(source)
        size_bytes = source.stat().st_size
        blob = self._blob(path)
        blob.metadata = {
            "sha256": payload_digest,
            "size_bytes": str(size_bytes),
        }
        try:
            blob.upload_from_filename(str(source), if_generation_match=0)
            uploaded = True
        except PreconditionFailed:
            uploaded = False
        stored = self.describe(path)
        if stored.content_sha256 != payload_digest or stored.size_bytes != size_bytes:
            msg = "immutable Stage 12 cache path contains other data"
            raise RuntimeError(msg)
        return uploaded, stored

    def upload_bytes(
        self,
        path: str,
        payload: bytes,
        *,
        content_type: str,
    ) -> tuple[bool, CacheObject]:
        """Upload ``payload`` to ``path`` unless an object already exists there.

        Returns whether this call uploaded it and the stored object. Raises
        ``RuntimeError`` when the stored object's digest or size differs from
        the payload's.
        """
        digest = sha256(payload).hexdigest()
        blob = self._blob(path)
        blob.metadata = {"sha256": digest, "size_bytes": str(len(payload))}
        try:
            blob.upload_from_string(
                payload,
                content_type=content_type,
                if_generation_match=0,
            )
            uploaded = True
        except PreconditionFailed:
            uploaded = False
        stored = self.describe(path)
        if stored.content_sha256 != digest or stored.size_bytes != len(payload):
            msg = "immutable Stage 12 cache path contains other data"
            raise RuntimeError(msg)
        return uploaded, stored

    def describe(self, path: str) -> CacheObject:
        """Return the stored object's digest and size.

        Uses the object's metadata, or hashes its content when the metadata
        lacks them. Raises ``FileNotFoundError`` when there is no object.
        """
        blob = self._blob(path)
        try:
            blob.reload()
        except NotFound:
            raise FileNotFoundError(path) from None
        metadata = blob.metadata or {}
        digest = metadata.get("sha256")
        raw_size = metadata.get("size_bytes")
        if not isinstance(digest, str) or not isinstance(raw_size, str):
            payload = blob.download_as_bytes()
            digest = sha256(payload).hexdigest()
            raw_size = str(len(payload))
        return CacheObject(
            path=path,
            content_sha256=digest,
            size_bytes=int(raw_size),
        )

    def download_file(self, path: str, local_path: str | Path) -> CacheObject:
        """Download ``path`` to ``local_path`` and check its digest.

        The file is written beside the destination and moved into place only
        after its digest matches, so a failed download leaves no file.
        """
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".partial")
        try:
            self._blob(path).download_to_filename(str(partial))
            stored = self.describe(path)
            if _file_sha256(partial) != stored.content_sha256:
                msg = "downloaded Stage 12 cache digest differs"
                raise RuntimeError(msg)
            partial.replace(destination)
            return stored
        finally:
            partial.unlink(missing_ok=True)

    def write_json(self, path: str, payload: Mapping[str, Any]) -> None:
        """Store ``payload`` at ``path`` as canonical JSON, as ``upload_bytes`` does."""
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        self.upload_bytes(path, encoded, content_type="application/json")

    def write_manifest(self, payload: Mapping[str, Any]) -> str:
        """Store a manifest under its canonical digest and return the digest."""
        digest = canonical_digest(payload)
        self.write_json(f"manifests/{digest}.json", payload)
        return digest

    def read_manifest(self, digest: str) -> Mapping[str, Any] | None:
        """Return the manifest stored under ``digest``, or None if it is missing.

        A stored value that is not a JSON object also gives None.
        """
        try:
            payload = self._blob(f"manifests/{digest}.json").download_as_bytes()
        except NotFound:
            return None
        value = json.loads(payload)
        return value if isinstance(value, Mapping) else None
