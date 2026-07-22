"""Store-client tests against an injected fake GCS client (no network).

The load-bearing behaviors: write-once semantics (``if_generation_match=0``
sent, ``PreconditionFailed`` reported as already-present success), marker
overwrites, and manifest content-addressing.
"""

import json
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed

from policyengine_simulation_executor import artifact_keys as ak
from policyengine_simulation_executor.artifact_store import (
    ARTIFACT_BUCKET_ENV,
    ArtifactStore,
    resolve_bucket_name,
)


class FakeBlob:
    def __init__(self, store, path):
        self.store = store
        self.path = path

    def exists(self):
        return self.path in self.store.objects

    def _write(self, data, **kwargs):
        self.store.upload_calls.append((self.path, kwargs))
        if kwargs.get("if_generation_match") == 0 and self.path in self.store.objects:
            raise PreconditionFailed(f"object exists: {self.path}")
        self.store.objects[self.path] = data

    def upload_from_filename(self, filename, **kwargs):
        self._write(open(filename, "rb").read(), **kwargs)

    def upload_from_string(self, data, **kwargs):
        self._write(data.encode() if isinstance(data, str) else data, **kwargs)

    def download_to_filename(self, filename):
        if self.path not in self.store.objects:
            raise NotFound(self.path)
        with open(filename, "wb") as handle:
            handle.write(self.store.objects[self.path])

    def download_as_bytes(self):
        if self.path not in self.store.objects:
            raise NotFound(self.path)
        return self.store.objects[self.path]


class FakeClient:
    def __init__(self):
        self.objects = {}
        self.upload_calls = []
        self.bucket_names = []

    def bucket(self, name):
        self.bucket_names.append(name)
        return SimpleNamespace(blob=lambda path: FakeBlob(self, path))


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def store(fake_client):
    return ArtifactStore("test-bucket", client=fake_client)


def test_bucket_name_resolution(monkeypatch):
    monkeypatch.delenv(ARTIFACT_BUCKET_ENV, raising=False)
    with pytest.raises(RuntimeError, match=ARTIFACT_BUCKET_ENV):
        resolve_bucket_name()
    monkeypatch.setenv(ARTIFACT_BUCKET_ENV, "from-env")
    assert resolve_bucket_name() == "from-env"
    assert resolve_bucket_name("explicit") == "explicit"


def test_gcs_client_importable():
    """Canary for the explicit google-cloud-storage dependency."""
    import google.cloud.storage  # noqa: F401


def test_upload_is_write_once(store, fake_client, tmp_path):
    payload = tmp_path / "artifact.h5"
    payload.write_bytes(b"bytes-one")

    assert store.upload_file("baselines/us/d/bl1-a.h5", payload) is True
    # Every artifact upload must carry the write-once precondition.
    assert fake_client.upload_calls[-1][1]["if_generation_match"] == 0

    payload.write_bytes(b"bytes-two")
    assert store.upload_file("baselines/us/d/bl1-a.h5", payload) is False
    assert fake_client.objects["baselines/us/d/bl1-a.h5"] == b"bytes-one"


def test_download_creates_parent_dirs(store, fake_client, tmp_path):
    fake_client.objects["datasets/us/d/populace_year_2026.h5"] = b"content"
    destination = tmp_path / "nested" / "dir" / "populace_year_2026.h5"
    store.download_file("datasets/us/d/populace_year_2026.h5", destination)
    assert destination.read_bytes() == b"content"


def test_exists(store, fake_client):
    assert store.exists("manifests/x.json") is False
    fake_client.objects["manifests/x.json"] = b"{}"
    assert store.exists("manifests/x.json") is True


def test_read_json_missing_returns_none(store):
    assert store.read_json("deployed/beta.json") is None


def test_manifest_roundtrip_is_content_addressed(store, fake_client):
    payload = {"artifacts": ["a", "b"], "receipt": {"policyengine_version": "4.22.0"}}
    digest = store.write_manifest(payload)
    assert digest == ak.canonical_digest(payload)
    assert store.read_manifest(digest) == payload
    # Manifests are write-once too: same content is a no-op, and the
    # stored bytes remain the first write's.
    stored_before = dict(fake_client.objects)
    store.write_manifest(payload)
    assert fake_client.objects == stored_before


def test_deployed_marker_overwrites(store, fake_client):
    store.write_deployed_marker("beta", {"manifest": "m1"})
    store.write_deployed_marker("beta", {"manifest": "m2"})
    assert store.read_deployed_marker("beta") == {"manifest": "m2"}
    # Markers are pointers: no write-once precondition on the second write.
    marker_calls = [
        kwargs
        for path, kwargs in fake_client.upload_calls
        if path == "deployed/beta.json"
    ]
    assert all("if_generation_match" not in kwargs for kwargs in marker_calls)


def test_marker_payload_is_json(store, fake_client):
    store.write_deployed_marker("prod", {"manifest": "m1", "versions": {"us": "1"}})
    raw = fake_client.objects["deployed/prod.json"]
    assert json.loads(raw) == {"manifest": "m1", "versions": {"us": "1"}}
