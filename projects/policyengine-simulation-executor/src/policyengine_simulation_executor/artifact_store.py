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
from typing import Any, Optional

from google.api_core.exceptions import NotFound, PreconditionFailed

from policyengine_simulation_executor import artifact_keys

ARTIFACT_BUCKET_ENV = "POLICYENGINE_ARTIFACT_BUCKET"


def resolve_bucket_name(explicit: Optional[str] = None) -> str:
    bucket = explicit or os.environ.get(ARTIFACT_BUCKET_ENV)
    if not bucket:
        raise RuntimeError(
            f"No artifact bucket configured: pass one or set {ARTIFACT_BUCKET_ENV}."
        )
    return bucket


def _default_client():
    from google.cloud import storage

    from policyengine_simulation_executor.simulation_runtime import (
        setup_gcp_credentials,
    )

    # The client reads ADC at construction time, so the materialized
    # credentials file only needs to exist inside this block.
    with setup_gcp_credentials():
        return storage.Client()


class ArtifactStore:
    def __init__(self, bucket_name: Optional[str] = None, *, client: Any = None):
        self.bucket_name = resolve_bucket_name(bucket_name)
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = _default_client()
        return self._client

    def _blob(self, path: str):
        return self.client.bucket(self.bucket_name).blob(path)

    def exists(self, path: str) -> bool:
        return bool(self._blob(path).exists())

    def upload_file(self, path: str, local_path: str | Path) -> bool:
        """Write-once upload. Returns True if this call uploaded the object,
        False if an identical-keyed object already existed (see module
        docstring for why that is success)."""
        try:
            self._blob(path).upload_from_filename(
                str(local_path), if_generation_match=0
            )
        except PreconditionFailed:
            return False
        return True

    def download_file(self, path: str, local_path: str | Path) -> None:
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._blob(path).download_to_filename(str(destination))

    def _upload_json(self, path: str, payload: Mapping[str, Any], *, overwrite: bool):
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

    def read_json(self, path: str) -> Optional[Mapping[str, Any]]:
        try:
            payload = json.loads(self._blob(path).download_as_bytes())
        except NotFound:
            return None
        return payload if isinstance(payload, Mapping) else None

    # --- Manifests (write-once, content-addressed) ------------------------

    def write_manifest(self, payload: Mapping[str, Any]) -> str:
        """Store a manifest under its own content digest; returns the digest."""
        digest = artifact_keys.canonical_digest(payload)
        self._upload_json(
            artifact_keys.manifest_path(digest), payload, overwrite=False
        )
        return digest

    def read_manifest(self, digest: str) -> Optional[Mapping[str, Any]]:
        return self.read_json(artifact_keys.manifest_path(digest))

    # --- Deployed-environment markers (last-writer-wins) ------------------

    def write_deployed_marker(
        self, environment: str, payload: Mapping[str, Any]
    ) -> None:
        self._upload_json(
            artifact_keys.deployed_marker_path(environment), payload, overwrite=True
        )

    def read_deployed_marker(self, environment: str) -> Optional[Mapping[str, Any]]:
        return self.read_json(artifact_keys.deployed_marker_path(environment))
