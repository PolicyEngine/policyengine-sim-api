"""Dedicated write-once object store for Stage 12 cache files."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any

from google.api_core.exceptions import NotFound, PreconditionFailed

from .keys import canonical_digest
from .models import CacheObject

CACHE_BUCKET_ENV = "STAGE12_CACHE_BUCKET"


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_cache_bucket(explicit: str | None = None) -> str:
    bucket = explicit or os.environ.get(CACHE_BUCKET_ENV)
    if not bucket:
        raise RuntimeError(
            f"Stage 12 cache bucket is not configured; set {CACHE_BUCKET_ENV}"
        )
    return bucket


def _default_client():
    from google.cloud import storage
    from policyengine_simulation_executor.simulation_runtime import (
        setup_gcp_credentials,
    )

    with setup_gcp_credentials():
        return storage.Client()


class Stage12CacheStore:
    def __init__(self, bucket_name: str | None = None, *, client: Any = None):
        self.bucket_name = resolve_cache_bucket(bucket_name)
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = _default_client()
        return self._client

    def _blob(self, path: str):
        return self.client.bucket(self.bucket_name).blob(path)

    def exists(self, path: str) -> bool:
        return bool(self._blob(path).exists())

    def upload_file(
        self, path: str, local_path: str | Path
    ) -> tuple[bool, CacheObject]:
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
            raise RuntimeError("immutable Stage 12 cache path contains other data")
        return uploaded, stored

    def upload_bytes(
        self,
        path: str,
        payload: bytes,
        *,
        content_type: str,
    ) -> tuple[bool, CacheObject]:
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
            raise RuntimeError("immutable Stage 12 cache path contains other data")
        return uploaded, stored

    def describe(self, path: str) -> CacheObject:
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
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".partial")
        try:
            self._blob(path).download_to_filename(str(partial))
            stored = self.describe(path)
            if _file_sha256(partial) != stored.content_sha256:
                raise RuntimeError("downloaded Stage 12 cache digest differs")
            partial.replace(destination)
            return stored
        finally:
            partial.unlink(missing_ok=True)

    def write_manifest(self, payload: Mapping[str, Any]) -> str:
        digest = canonical_digest(payload)
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        self.upload_bytes(
            f"manifests/{digest}.json",
            encoded,
            content_type="application/json",
        )
        return digest

    def read_manifest(self, digest: str) -> Mapping[str, Any] | None:
        try:
            payload = self._blob(f"manifests/{digest}.json").download_as_bytes()
        except NotFound:
            return None
        value = json.loads(payload)
        return value if isinstance(value, Mapping) else None
