"""Self-contained Stage 12 cache fetch used during Modal image construction."""

from __future__ import annotations


def fetch_stage12_cache(bucket: str, manifest: dict | None, *, client=None) -> None:
    """Download and verify every manifest-listed cache file into the image."""

    from hashlib import sha256
    import json
    import os
    from pathlib import Path

    if not bucket or not manifest:
        raise RuntimeError("Stage 12 cache deployment has no bucket or manifest")
    if manifest.get("schema") != "stage12-mf1":
        raise RuntimeError("Stage 12 cache manifest schema is unsupported")
    bundle = manifest.get("bundle")
    if not isinstance(bundle, dict) or bundle.get(
        "bundle_manifest_sha256"
    ) != os.getenv("STAGE12_BUNDLE_MANIFEST_SHA256"):
        raise RuntimeError("Stage 12 cache manifest names another installed bundle")
    if manifest.get("years") != [2026, 2027, 2025]:
        raise RuntimeError("Stage 12 cache manifest has an unexpected year set")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise RuntimeError("Stage 12 cache manifest has no artifact list")
    type_counts = {
        kind: sum(
            item.get("type") == kind for item in artifacts if isinstance(item, dict)
        )
        for kind in ("dataset", "baseline")
    }
    if type_counts != {"dataset": 3, "baseline": 60}:
        raise RuntimeError("Stage 12 cache manifest has an unexpected artifact count")
    canonical = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    manifest_digest = sha256(canonical).hexdigest()
    if manifest_digest != os.getenv("STAGE12_CACHE_MANIFEST_DIGEST"):
        raise RuntimeError("Stage 12 cache manifest digest differs from deployment")

    if client is None:
        credentials = {}
        raw_credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        if raw_credentials:
            decoded = json.loads(raw_credentials)
            info = json.loads(decoded) if isinstance(decoded, str) else decoded
            from google.oauth2 import service_account

            credentials = {
                "credentials": service_account.Credentials.from_service_account_info(
                    info
                ),
                "project": info.get("project_id"),
            }
        from google.cloud import storage

        client = storage.Client(**credentials)

    data_dir = Path(os.environ["POLICYENGINE_DATA_FOLDER"])
    data_dir.mkdir(parents=True, exist_ok=True)
    filenames: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise RuntimeError("Stage 12 cache artifact entry is invalid")
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
            raise RuntimeError("Stage 12 cache artifact metadata is invalid")
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
                raise RuntimeError("Stage 12 cache artifact size differs")
            if digest.hexdigest() != expected_digest:
                raise RuntimeError("Stage 12 cache artifact digest differs")
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)
    (data_dir / ".stage12-cache-manifest.json").write_bytes(canonical)
