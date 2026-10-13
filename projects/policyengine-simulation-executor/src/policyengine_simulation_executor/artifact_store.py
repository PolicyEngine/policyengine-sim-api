"""GCS-backed content-addressed store for precomputed simulation artifacts.

Object paths come from ``artifact_keys`` (the single source of truth for
layout). Two write disciplines coexist:

* **Artifacts and manifests are write-once.** Uploads pass
  ``if_generation_match=0`` so the first writer wins and a concurrent
  duplicate write (e.g. the beta and prod deploy legs racing on the same
  key) fails the precondition instead of clobbering. Because a key digests
  the full input closure, the loser's bytes are identical to the winner's,
  so ``PreconditionFailed`` is reported as success-without-upload.
* **Deployed-environment markers are last-writer-wins.** They are pointers,
  not artifacts; each successful deploy overwrites its environment's marker.

Credentials: the store materializes ``GOOGLE_APPLICATION_CREDENTIALS_JSON``
via the existing ``setup_gcp_credentials`` helper only while constructing
the real client; injected fake clients (tests) never touch credentials or
the network.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.api_core.exceptions import NotFound, PreconditionFailed

from policyengine_simulation_executor import artifact_keys

if TYPE_CHECKING:
    from google.cloud import storage

ARTIFACT_BUCKET_ENV = "POLICYENGINE_ARTIFACT_BUCKET"


def resolve_bucket_name(explicit: str | None = None) -> str:
    """Return ``explicit`` or the ``POLICYENGINE_ARTIFACT_BUCKET`` value.

    Raises ``RuntimeError`` when neither names a bucket.
    """
    bucket = explicit or os.environ.get(ARTIFACT_BUCKET_ENV)
    if not bucket:
        msg = f"No artifact bucket configured: pass one or set {ARTIFACT_BUCKET_ENV}."
        raise RuntimeError(msg)
    return bucket


def _default_client() -> storage.Client:
    from google.cloud import (  # noqa: PLC0415  # google-cloud-storage loads only when a default client is built
        storage,
    )

    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime.setup_gcp_credentials
        setup_gcp_credentials,
    )

    # The client reads ADC at construction time, so the materialized
    # credentials file only needs to exist inside this block.
    with setup_gcp_credentials():
        return storage.Client()


class ArtifactStore:
    """Artifact, manifest and deployed-marker storage in one GCS bucket."""

    def __init__(
        self, bucket_name: str | None = None, *, client: storage.Client | None = None
    ) -> None:
        """Resolve the bucket now; the real GCS client is built lazily on first use."""
        self.bucket_name = resolve_bucket_name(bucket_name)
        self._client = client

    @property
    def client(self) -> storage.Client:
        """GCS client: the injected one, or a real client built on first access."""
        if self._client is None:
            self._client = _default_client()
        return self._client

    def _blob(self, path: str) -> storage.Blob:
        return self.client.bucket(self.bucket_name).blob(path)

    def exists(self, path: str) -> bool:
        """Report whether an object is stored at ``path``."""
        return bool(self._blob(path).exists())

    def upload_file(self, path: str, local_path: str | Path) -> bool:
        """Upload a local file write-once; return whether this call created the object.

        False means an object already existed at that key, which counts as
        success because keys digest the full input closure (see the module
        docstring).
        """
        try:
            self._blob(path).upload_from_filename(
                str(local_path), if_generation_match=0
            )
        except PreconditionFailed:
            return False
        return True

    def upload_bytes(
        self,
        path: str,
        payload: bytes,
        *,
        content_type: str,
    ) -> bool:
        """Write immutable bytes and report whether this call created them."""
        try:
            self._blob(path).upload_from_string(
                payload,
                content_type=content_type,
                if_generation_match=0,
            )
        except PreconditionFailed:
            return False
        return True

    def read_bytes(self, path: str) -> bytes | None:
        """Return the object's bytes, or None when nothing is stored at ``path``."""
        try:
            return self._blob(path).download_as_bytes()
        except NotFound:
            return None

    def download_file(self, path: str, local_path: str | Path) -> None:
        """Download ``path`` to ``local_path`` atomically, creating parent directories.

        A failed download never leaves a partial file at ``local_path``.
        """
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Download to a sibling temp name and rename: callers guard on
        # exists(), so a mid-stream failure must never leave a truncated
        # file at the final name (it would poison the container's later
        # work for the rest of the run).
        partial = destination.with_name(destination.name + ".partial")
        try:
            self._blob(path).download_to_filename(str(partial))
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        partial.replace(destination)

    def _upload_json(
        self, path: str, payload: Mapping[str, Any], *, overwrite: bool
    ) -> bool:
        data = json.dumps(payload, sort_keys=True, indent=2)
        blob = self._blob(path)
        if overwrite:
            blob.upload_from_string(data, content_type="application/json")
            return True
        try:
            blob.upload_from_string(
                data, content_type="application/json", if_generation_match=0
            )
        except PreconditionFailed:
            return False
        return True

    def read_json(self, path: str) -> Mapping[str, Any] | None:
        """Return the JSON object at ``path``; None if missing or not an object."""
        try:
            payload = json.loads(self._blob(path).download_as_bytes())
        except NotFound:
            return None
        return payload if isinstance(payload, Mapping) else None

    # --- Manifests (write-once, content-addressed) ------------------------

    def write_manifest(self, payload: Mapping[str, Any]) -> str:
        """Store a manifest under its own content digest; returns the digest."""
        digest = artifact_keys.canonical_digest(payload)
        self._upload_json(artifact_keys.manifest_path(digest), payload, overwrite=False)
        return digest

    def read_manifest(self, digest: str) -> Mapping[str, Any] | None:
        """Return the manifest stored under ``digest``, or None if absent."""
        return self.read_json(artifact_keys.manifest_path(digest))

    # --- Deployed-environment markers (last-writer-wins) ------------------

    def write_deployed_marker(
        self, environment: str, payload: Mapping[str, Any]
    ) -> None:
        """Overwrite ``environment``'s deployed marker with ``payload``."""
        self._upload_json(
            artifact_keys.deployed_marker_path(environment), payload, overwrite=True
        )

    def read_deployed_marker(self, environment: str) -> Mapping[str, Any] | None:
        """Return ``environment``'s deployed marker, or None if none was written."""
        return self.read_json(artifact_keys.deployed_marker_path(environment))
