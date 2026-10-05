"""Resolve a content-addressed precompute manifest during Modal deployment."""

from __future__ import annotations

import os


def deploy_time_artifact_inputs() -> tuple[str, dict | None]:
    """Return the artifact bucket and validated manifest on the deploy runner."""

    import modal

    if not modal.is_local():
        return "", None
    digest = os.environ.get("POLICYENGINE_MANIFEST_DIGEST")
    bucket = os.environ.get("POLICYENGINE_ARTIFACT_BUCKET", "")
    if not digest:
        return bucket, None

    from policyengine_simulation_executor.artifact_store import ArtifactStore
    from policyengine_simulation_executor.precompute_models import ArtifactManifest

    store = ArtifactStore(bucket or None)
    payload = store.read_manifest(digest)
    if payload is None:
        raise RuntimeError(
            f"Artifact manifest {digest} is not in the store: the deploy "
            "must consume a digest published by the precompute run."
        )
    manifest = ArtifactManifest.model_validate(payload)
    return store.bucket_name, manifest.canonical_payload()
