"""Tests for the self-contained Stage 12 image cache fetch."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.modal.stage12_cache_image import fetch_stage12_cache


class _Blob:
    def __init__(self, payload: bytes):
        self.payload = payload

    def download_to_filename(self, filename: str) -> None:
        Path(filename).write_bytes(self.payload)


class _Client:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads

    def bucket(self, _name: str):
        return SimpleNamespace(blob=lambda path: _Blob(self.payloads[path]))


def _manifest() -> tuple[dict, dict[str, bytes]]:
    artifacts = []
    payloads = {}
    for kind, count in (("dataset", 3), ("baseline", 60)):
        for index in range(count):
            filename = f"{kind}-{index}.h5"
            path = f"{kind}s/us/{index}/{filename}"
            payload = f"{kind}-{index}".encode()
            payloads[path] = payload
            artifacts.append(
                {
                    "type": kind,
                    "path": path,
                    "filename": filename,
                    "year": (2026, 2027, 2025)[index % 3],
                    "identity_digest": f"identity-{kind}-{index}",
                    "content_sha256": sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
            )
    return (
        {
            "schema": "stage12-mf1",
            "country": "us",
            "years": [2026, 2027, 2025],
            "partition_sha256": "p" * 64,
            "bundle": {
                "bundle_manifest_sha256": "b" * 64,
                "policyengine_version": "5.2.0",
                "core_package_version": "3.0.0",
                "country_package_version": "1.0.0",
                "data_package_version": "2.0.0",
                "data_artifact_revision": "revision",
                "default_dataset": "populace_us_2024",
            },
            "artifacts": artifacts,
        },
        payloads,
    )


def _digest(manifest: dict) -> str:
    return sha256(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def test_fetches_and_verifies_all_cache_files(monkeypatch, tmp_path) -> None:
    manifest, payloads = _manifest()
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    monkeypatch.setenv("STAGE12_BUNDLE_MANIFEST_SHA256", "b" * 64)
    monkeypatch.setenv("STAGE12_CACHE_MANIFEST_DIGEST", _digest(manifest))

    fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))

    assert len(list(tmp_path.glob("*.h5"))) == 63
    assert (tmp_path / ".stage12-cache-manifest.json").exists()


def test_rejects_foreign_or_corrupt_cache(monkeypatch, tmp_path) -> None:
    manifest, payloads = _manifest()
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(tmp_path))
    monkeypatch.setenv("STAGE12_BUNDLE_MANIFEST_SHA256", "different")
    monkeypatch.setenv("STAGE12_CACHE_MANIFEST_DIGEST", _digest(manifest))
    with pytest.raises(RuntimeError, match="another installed bundle"):
        fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))

    monkeypatch.setenv("STAGE12_BUNDLE_MANIFEST_SHA256", "b" * 64)
    corrupt_path = manifest["artifacts"][0]["path"]
    payloads[corrupt_path] = b"corrupt"
    with pytest.raises(RuntimeError, match="size differs|digest differs"):
        fetch_stage12_cache("cache-bucket", manifest, client=_Client(payloads))
