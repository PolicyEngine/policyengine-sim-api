"""Artifact-fetch layer tests against an injected fake GCS client.

The load-bearing behaviors: every manifest artifact lands in the data
folder under its runtime filename, missing store objects fail the build
loudly before anything downloads, the freshness gate rejects manifests
computed for a different version-set, and the module stays importable
without the executor package (the layer runs before
add_local_python_source).
"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.modal._image_setup as image_setup
from policyengine_simulation_executor.precompute_models import ArtifactManifest
from src.modal._image_setup import fetch_artifacts


class FakeBlob:
    def __init__(self, store, path):
        self.store = store
        self.path = path

    def exists(self):
        return self.path in self.store.objects

    def download_to_filename(self, filename):
        self.store.downloads.append(self.path)
        Path(filename).write_bytes(self.store.objects[self.path])


class FakeGcsClient:
    def __init__(self):
        self.objects = {}
        self.downloads = []
        self.bucket_names = []

    def bucket(self, name):
        self.bucket_names.append(name)
        return SimpleNamespace(blob=lambda path: FakeBlob(self, path))


def _manifest():
    """The wire shape fetch_artifacts consumes, derived from the schema
    (the layer itself cannot import the model, so it speaks the canonical
    dict — building the fake through ArtifactManifest keeps it honest)."""
    return ArtifactManifest.model_validate(
        {
            "schema": "mf1",
            "country": "us",
            "receipt": {
                "policyengine_version": "4.22.0",
                "model_version": "1.3.0",
                "data_version": "1.2.3",
                "data_artifact_revision": "rev-abc",
                "default_dataset": "populace_cps",
            },
            "artifacts": [
                {
                    "type": "dataset",
                    "path": "datasets/us/d1/populace_year_2026.h5",
                    "filename": "populace_year_2026.h5",
                    "year": 2026,
                    "digest": "d1",
                },
                {
                    "type": "baseline",
                    "path": "baselines/us/b1/bl1-aaaa.h5",
                    "filename": "bl1-aaaa.h5",
                    "year": 2026,
                    "digest": "b1",
                },
            ],
        }
    ).canonical_payload()


@pytest.fixture
def fake_client():
    client = FakeGcsClient()
    for artifact in _manifest()["artifacts"]:
        client.objects[artifact["path"]] = f"bytes-{artifact['digest']}".encode()
    return client


@pytest.fixture
def data_folder(monkeypatch, tmp_path):
    folder = tmp_path / "data"
    monkeypatch.setenv("POLICYENGINE_DATA_FOLDER", str(folder))
    monkeypatch.setenv("POLICYENGINE_VERSION", "4.22.0")
    monkeypatch.setenv("POLICYENGINE_US_VERSION", "1.3.0")
    monkeypatch.delenv("POLICYENGINE_BUNDLE_RECEIPT", raising=False)
    return folder


class TestFetchArtifacts:
    def test_downloads_every_artifact_under_its_runtime_filename(
        self, fake_client, data_folder
    ):
        fetch_artifacts("test-bucket", _manifest(), client=fake_client)
        assert "test-bucket" in fake_client.bucket_names
        for artifact in _manifest()["artifacts"]:
            target = data_folder / artifact["filename"]
            assert target.read_bytes() == f"bytes-{artifact['digest']}".encode()

    def test_missing_store_object_fails_before_any_download(
        self, fake_client, data_folder
    ):
        missing_path = _manifest()["artifacts"][1]["path"]
        del fake_client.objects[missing_path]
        with pytest.raises(RuntimeError, match=missing_path):
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)
        assert fake_client.downloads == []

    def test_unresolved_manifest_fails_with_the_digest_env_var_named(
        self, fake_client, data_folder
    ):
        with pytest.raises(RuntimeError, match="POLICYENGINE_MANIFEST_DIGEST"):
            fetch_artifacts("test-bucket", None, client=fake_client)
        assert fake_client.downloads == []

    def test_overwrites_a_preexisting_same_named_file(self, fake_client, data_folder):
        """No skip-if-exists: stale same-named bytes must never survive
        into the image."""
        data_folder.mkdir(parents=True)
        stale = data_folder / "populace_year_2026.h5"
        stale.write_bytes(b"stale")
        fetch_artifacts("test-bucket", _manifest(), client=fake_client)
        assert stale.read_bytes() == b"bytes-d1"


class TestFreshnessGate:
    def test_rejects_a_policyengine_version_mismatch(
        self, monkeypatch, fake_client, data_folder
    ):
        monkeypatch.setenv("POLICYENGINE_VERSION", "4.99.0")
        with pytest.raises(RuntimeError, match=r"4\.99\.0.*4\.22\.0"):
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)
        assert fake_client.downloads == []

    def test_rejects_a_model_version_mismatch(
        self, monkeypatch, fake_client, data_folder
    ):
        monkeypatch.setenv("POLICYENGINE_US_VERSION", "9.9.9")
        with pytest.raises(RuntimeError, match="POLICYENGINE_US_VERSION"):
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)

    def test_rejects_an_installed_data_version_mismatch(
        self, monkeypatch, tmp_path, fake_client, data_folder
    ):
        receipt_file = tmp_path / "bundle-receipt.json"
        receipt_file.write_text('{"datasets": [{"country": "us", "version": "9.9.9"}]}')
        monkeypatch.setenv("POLICYENGINE_BUNDLE_RECEIPT", str(receipt_file))
        with pytest.raises(RuntimeError, match=r"9\.9\.9.*1\.2\.3"):
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)

    def test_passes_when_the_installed_data_version_matches(
        self, monkeypatch, tmp_path, fake_client, data_folder
    ):
        receipt_file = tmp_path / "bundle-receipt.json"
        receipt_file.write_text('{"datasets": [{"country": "us", "version": "1.2.3"}]}')
        monkeypatch.setenv("POLICYENGINE_BUNDLE_RECEIPT", str(receipt_file))
        fetch_artifacts("test-bucket", _manifest(), client=fake_client)
        assert len(fake_client.downloads) == 2

    def test_proceeds_without_an_installed_receipt_file(
        self, monkeypatch, tmp_path, fake_client, data_folder
    ):
        """Lenient by design: local fault-drill runs have no bundle
        receipt file."""
        monkeypatch.setenv(
            "POLICYENGINE_BUNDLE_RECEIPT", str(tmp_path / "does-not-exist.json")
        )
        fetch_artifacts("test-bucket", _manifest(), client=fake_client)
        assert len(fake_client.downloads) == 2


class TestCredentialShim:
    """The client=None path: credentials are built in memory from the
    secret blob via service_account.Credentials.from_service_account_info
    (never written to disk — the layer's filesystem is committed into the
    image), falling back to ambient ADC when no blob is present."""

    @pytest.fixture
    def patched_google(self, monkeypatch, fake_client):
        from google.cloud import storage
        from google.oauth2 import service_account

        recorder = SimpleNamespace(infos=[], client_calls=[], sentinel=object())

        def fake_from_info(info):
            recorder.infos.append(info)
            return recorder.sentinel

        def fake_client_factory(**kwargs):
            recorder.client_calls.append(kwargs)
            return fake_client

        monkeypatch.setattr(
            service_account.Credentials, "from_service_account_info", fake_from_info
        )
        monkeypatch.setattr(storage, "Client", fake_client_factory)
        return recorder

    def test_builds_in_memory_credentials_from_the_secret_blob(
        self, monkeypatch, fake_client, data_folder, patched_google
    ):
        monkeypatch.setenv(
            "GOOGLE_APPLICATION_CREDENTIALS_JSON",
            '{"type": "service_account", "project_id": "pe-test"}',
        )

        fetch_artifacts("test-bucket", _manifest())

        assert patched_google.infos == [
            {"type": "service_account", "project_id": "pe-test"}
        ]
        assert patched_google.client_calls == [
            {"credentials": patched_google.sentinel, "project": "pe-test"}
        ]
        assert len(fake_client.downloads) == 2

    def test_unwraps_the_double_encoded_blob(
        self, monkeypatch, fake_client, data_folder, patched_google
    ):
        clean = json.dumps({"type": "service_account", "project_id": "pe-test"})
        monkeypatch.setenv(
            "GOOGLE_APPLICATION_CREDENTIALS_JSON", clean.replace('"', '\\"')
        )

        fetch_artifacts("test-bucket", _manifest())

        assert patched_google.infos == [
            {"type": "service_account", "project_id": "pe-test"}
        ]

    def test_falls_back_to_ambient_adc_without_a_blob(
        self, monkeypatch, fake_client, data_folder, patched_google
    ):
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)

        fetch_artifacts("test-bucket", _manifest())

        assert patched_google.infos == []
        assert patched_google.client_calls == [{}]


def test_fetch_never_writes_credentials_to_disk():
    """A run_function layer commits its container filesystem into the
    image, so the shim must never materialize the key as a file."""
    source = Path(image_setup.__file__).read_text(encoding="utf-8")
    assert "tempfile" not in source


def test_image_setup_module_stays_self_contained():
    """The fetch layer runs before add_local_python_source, so the module
    must have no module-level imports and must never import the executor
    package, even lazily inside a function body."""
    source = Path(image_setup.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    module_level_imports = [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert module_level_imports == []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(
                "policyengine_simulation_executor"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("policyengine_simulation_executor")
