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
import sys
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
    """Return the wire shape fetch_artifacts consumes, derived from the schema.

    The layer itself cannot import the model, so it speaks the canonical
    dict — building the fake through ArtifactManifest keeps it honest.
    """
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

    @pytest.mark.usefixtures("data_folder")
    def test_missing_store_object_fails_before_any_download(self, fake_client):
        missing_path = _manifest()["artifacts"][1]["path"]
        del fake_client.objects[missing_path]
        with pytest.raises(RuntimeError, match=missing_path):
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)
        assert fake_client.downloads == []

    @pytest.mark.usefixtures("data_folder")
    def test_unresolved_manifest_fails_with_the_digest_env_var_named(self, fake_client):
        with pytest.raises(RuntimeError, match="POLICYENGINE_MANIFEST_DIGEST"):
            fetch_artifacts("test-bucket", None, client=fake_client)
        assert fake_client.downloads == []

    def test_overwrites_a_preexisting_same_named_file(self, fake_client, data_folder):
        """No skip-if-exists: stale same-named bytes must never reach the image."""
        data_folder.mkdir(parents=True)
        stale = data_folder / "populace_year_2026.h5"
        stale.write_bytes(b"stale")
        fetch_artifacts("test-bucket", _manifest(), client=fake_client)
        assert stale.read_bytes() == b"bytes-d1"


def _state_dataset_artifact(state_code, *, folder=None, filename=None):
    digest = f"s-{state_code}"
    return {
        "type": "state_dataset",
        "path": f"state-datasets/us/{digest}/populace_local_year_2026.h5",
        "filename": filename or "populace_local_year_2026.h5",
        "folder": folder or f"state-datasets/populace_local/{state_code}",
        "state_code": state_code,
        "year": 2026,
        "digest": digest,
    }


def _manifest_with_state_datasets(*state_artifacts):
    manifest = _manifest()
    manifest["artifacts"] = [*manifest["artifacts"], *state_artifacts]
    return manifest


def _store_every_artifact(client, manifest):
    for artifact in manifest["artifacts"]:
        client.objects[artifact["path"]] = f"bytes-{artifact['digest']}".encode()


class TestStateDatasetPlacement:
    """Entries with a ``folder`` land in it; every state shares one filename."""

    def test_places_each_state_file_in_its_own_folder(self, fake_client, data_folder):
        manifest = _manifest_with_state_datasets(
            _state_dataset_artifact("UT"), _state_dataset_artifact("WY")
        )
        _store_every_artifact(fake_client, manifest)

        fetch_artifacts("test-bucket", manifest, client=fake_client)

        state_folder = data_folder / "state-datasets" / "populace_local"
        assert (state_folder / "UT" / "populace_local_year_2026.h5").read_bytes() == (
            b"bytes-s-UT"
        )
        assert (state_folder / "WY" / "populace_local_year_2026.h5").read_bytes() == (
            b"bytes-s-WY"
        )
        assert (data_folder / "populace_year_2026.h5").read_bytes() == b"bytes-d1"
        assert not (data_folder / "populace_local_year_2026.h5").exists()

    @pytest.mark.parametrize(
        "folder",
        [
            "/abs/state-datasets/UT",
            "state-datasets/../../escape",
            "state-datasets/./UT",
            "state-datasets//UT",
            "state-datasets\\UT",
            "",
        ],
    )
    @pytest.mark.usefixtures("data_folder")
    def test_refuses_an_unsafe_folder_before_any_download(self, fake_client, folder):
        manifest = _manifest_with_state_datasets(
            _state_dataset_artifact("UT", folder=folder or None)
        )
        manifest["artifacts"][-1]["folder"] = folder
        _store_every_artifact(fake_client, manifest)

        with pytest.raises(RuntimeError, match="folder"):
            fetch_artifacts("test-bucket", manifest, client=fake_client)
        assert fake_client.downloads == []

    @pytest.mark.parametrize(
        "filename", ["../populace_local_year_2026.h5", "UT/x.h5", "..", "."]
    )
    @pytest.mark.usefixtures("data_folder")
    def test_refuses_an_unsafe_filename_before_any_download(
        self, fake_client, filename
    ):
        manifest = _manifest_with_state_datasets(
            _state_dataset_artifact("UT", filename=filename)
        )
        _store_every_artifact(fake_client, manifest)

        with pytest.raises(RuntimeError, match="filename"):
            fetch_artifacts("test-bucket", manifest, client=fake_client)
        assert fake_client.downloads == []

    @pytest.mark.usefixtures("data_folder")
    def test_refuses_two_entries_for_one_file_before_any_download(self, fake_client):
        repeated = _state_dataset_artifact("UT")
        manifest = _manifest_with_state_datasets(
            repeated, {**repeated, "path": "state-datasets/us/other/x.h5"}
        )
        _store_every_artifact(fake_client, manifest)

        with pytest.raises(RuntimeError, match="more than once"):
            fetch_artifacts("test-bucket", manifest, client=fake_client)
        assert fake_client.downloads == []


class TestFreshnessGate:
    @pytest.mark.usefixtures("data_folder")
    def test_rejects_a_policyengine_version_mismatch(self, monkeypatch, fake_client):
        monkeypatch.setenv("POLICYENGINE_VERSION", "4.99.0")
        with pytest.raises(RuntimeError, match=r"4\.99\.0.*4\.22\.0"):
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)
        assert fake_client.downloads == []

    @pytest.mark.usefixtures("data_folder")
    def test_rejects_a_model_version_mismatch(self, monkeypatch, fake_client):
        monkeypatch.setenv("POLICYENGINE_US_VERSION", "9.9.9")
        with pytest.raises(RuntimeError, match="POLICYENGINE_US_VERSION"):
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)

    @pytest.mark.usefixtures("data_folder")
    def test_rejects_an_installed_data_version_mismatch(
        self, monkeypatch, tmp_path, fake_client
    ):
        receipt_file = tmp_path / "bundle-receipt.json"
        receipt_file.write_text('{"datasets": [{"country": "us", "version": "9.9.9"}]}')
        monkeypatch.setenv("POLICYENGINE_BUNDLE_RECEIPT", str(receipt_file))
        with pytest.raises(RuntimeError, match=r"9\.9\.9.*1\.2\.3"):
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)

    @pytest.mark.usefixtures("data_folder")
    def test_passes_when_the_installed_data_version_matches(
        self, monkeypatch, tmp_path, fake_client
    ):
        receipt_file = tmp_path / "bundle-receipt.json"
        receipt_file.write_text('{"datasets": [{"country": "us", "version": "1.2.3"}]}')
        monkeypatch.setenv("POLICYENGINE_BUNDLE_RECEIPT", str(receipt_file))
        fetch_artifacts("test-bucket", _manifest(), client=fake_client)
        assert len(fake_client.downloads) == 2

    @pytest.mark.usefixtures("data_folder")
    def test_proceeds_without_an_installed_receipt_file(
        self, monkeypatch, tmp_path, fake_client
    ):
        """Lenient by design: local fault-drill runs have no bundle receipt file."""
        monkeypatch.setenv(
            "POLICYENGINE_BUNDLE_RECEIPT", str(tmp_path / "does-not-exist.json")
        )
        fetch_artifacts("test-bucket", _manifest(), client=fake_client)
        assert len(fake_client.downloads) == 2


class TestCredentialShim:
    """The client=None path: credentials are built in memory from the secret blob.

    They come from service_account.Credentials.from_service_account_info
    (never written to disk — the layer's filesystem is committed into the
    image), falling back to ambient ADC when no blob is present.
    """

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

    @pytest.mark.usefixtures("data_folder")
    def test_builds_in_memory_credentials_from_the_secret_blob(
        self, monkeypatch, fake_client, patched_google
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

    @pytest.mark.usefixtures("fake_client", "data_folder")
    def test_unwraps_the_double_encoded_blob(self, monkeypatch, patched_google):
        clean = json.dumps({"type": "service_account", "project_id": "pe-test"})
        monkeypatch.setenv(
            "GOOGLE_APPLICATION_CREDENTIALS_JSON", clean.replace('"', '\\"')
        )

        fetch_artifacts("test-bucket", _manifest())

        assert patched_google.infos == [
            {"type": "service_account", "project_id": "pe-test"}
        ]

    @pytest.mark.usefixtures("fake_client", "data_folder")
    def test_falls_back_to_ambient_adc_without_a_blob(
        self, monkeypatch, patched_google
    ):
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)

        fetch_artifacts("test-bucket", _manifest())

        assert patched_google.infos == []
        assert patched_google.client_calls == [{}]

    @pytest.mark.usefixtures("fake_client", "data_folder")
    def test_unwraps_an_outer_quoted_blob(self, monkeypatch, patched_google):
        clean = json.dumps({"type": "service_account", "project_id": "pe-test"})
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", json.dumps(clean))

        fetch_artifacts("test-bucket", _manifest())

        assert patched_google.infos == [
            {"type": "service_account", "project_id": "pe-test"}
        ]

    @pytest.mark.usefixtures("fake_client", "data_folder")
    def test_rejects_a_blob_that_is_not_json(self, monkeypatch, patched_google):
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "not-json")

        with pytest.raises(json.JSONDecodeError):
            fetch_artifacts("test-bucket", _manifest())

        assert patched_google.infos == []
        assert patched_google.client_calls == []


def test_fetch_never_writes_credentials_to_disk():
    """The credential shim must never materialize the key as a file.

    A run_function layer commits its container filesystem into the image.
    """
    source = Path(image_setup.__file__).read_text(encoding="utf-8")
    assert "tempfile" not in source


def _top_level_module_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        return [(node.module or "").split(".")[0]]
    return [alias.name.split(".")[0] for alias in node.names]


def test_image_setup_module_stays_self_contained():
    """The image setup module must not import the executor package at all.

    The fetch layer runs before add_local_python_source, so the module may
    import only standard-library modules at module level and must never
    import the executor package, even lazily inside a function body.
    """
    source = Path(image_setup.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    module_level_imports = [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    non_standard_library_modules = [
        module_name
        for node in module_level_imports
        for module_name in _top_level_module_names(node)
        if module_name not in sys.stdlib_module_names
    ]
    assert non_standard_library_modules == []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(
                "policyengine_simulation_executor"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("policyengine_simulation_executor")


class TestCharacterization:
    """Pin the remaining fetch branches before the function is split up."""

    @pytest.mark.usefixtures("data_folder")
    @pytest.mark.parametrize(
        "receipt_text",
        [
            "not json",
            "[]",
            '{"datasets": ["not-an-entry"]}',
            '{"datasets": [{"country": "uk", "version": "9.9.9"}]}',
            '{"datasets": [{"country": "us"}]}',
        ],
    )
    def test_unusable_receipts_skip_the_data_version_check_with_a_warning(
        self, monkeypatch, tmp_path, fake_client, caplog, receipt_text
    ):
        import logging

        receipt_file = tmp_path / "bundle-receipt.json"
        receipt_file.write_text(receipt_text)
        monkeypatch.setenv("POLICYENGINE_BUNDLE_RECEIPT", str(receipt_file))

        with caplog.at_level(logging.INFO):
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)

        assert len(fake_client.downloads) == 2
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert warnings == [
            "No installed bundle receipt entry for us; "
            "skipping the data-version freshness check"
        ]

    @pytest.mark.usefixtures("data_folder")
    @pytest.mark.parametrize(
        ("versions", "passes"),
        [(["9.9.9", "1.2.3"], True), (["1.2.3", "9.9.9"], False)],
    )
    def test_the_last_receipt_entry_for_the_country_wins(
        self, monkeypatch, tmp_path, fake_client, versions, passes
    ):
        receipt_file = tmp_path / "bundle-receipt.json"
        receipt_file.write_text(
            json.dumps(
                {
                    "datasets": [
                        {"country": "us", "version": version} for version in versions
                    ]
                }
            )
        )
        monkeypatch.setenv("POLICYENGINE_BUNDLE_RECEIPT", str(receipt_file))

        if passes:
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)
            assert len(fake_client.downloads) == 2
        else:
            with pytest.raises(RuntimeError) as error:
                fetch_artifacts("test-bucket", _manifest(), client=fake_client)
            assert str(error.value) == (
                "Stale artifact manifest: installed us data version is '9.9.9' "
                "but the manifest was computed for '1.2.3'. Re-run the "
                "precompute for the current version-set."
            )

    def test_stale_policyengine_version_message(self, monkeypatch, fake_client):
        monkeypatch.setenv("POLICYENGINE_VERSION", "4.99.0")
        monkeypatch.setenv("POLICYENGINE_US_VERSION", "9.9.9")

        with pytest.raises(RuntimeError) as error:
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)

        assert str(error.value) == (
            "Stale artifact manifest: the image has POLICYENGINE_VERSION='4.99.0' "
            "but the manifest was computed for '4.22.0'. Re-run the precompute "
            "for the current version-set."
        )

    @pytest.mark.usefixtures("data_folder")
    def test_a_download_that_writes_nothing_fails_while_logging_it(self, fake_client):
        class SilentBlob(FakeBlob):
            def download_to_filename(self, filename):
                self.store.downloads.append((self.path, Path(filename).name))

        fake_client.bucket = lambda _name: SimpleNamespace(
            blob=lambda path: SilentBlob(fake_client, path)
        )

        with pytest.raises(FileNotFoundError):
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)

        assert fake_client.downloads == [
            ("datasets/us/d1/populace_year_2026.h5", "populace_year_2026.h5")
        ]

    def test_files_missing_after_every_download_fail_the_build(
        self, fake_client, data_folder
    ):
        class ReplacingBlob(FakeBlob):
            def download_to_filename(self, filename):
                for existing in data_folder.iterdir():
                    existing.unlink()
                super().download_to_filename(filename)

        fake_client.bucket = lambda _name: SimpleNamespace(
            blob=lambda path: ReplacingBlob(fake_client, path)
        )

        with pytest.raises(RuntimeError) as error:
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)

        missing = str(data_folder / "populace_year_2026.h5")
        assert str(error.value) == (
            f"Artifact fetch did not produce expected files: [{missing!r}]"
        )

    @pytest.mark.usefixtures("data_folder")
    def test_logs_each_fetched_artifact(self, fake_client, caplog):
        import logging

        with caplog.at_level(logging.INFO):
            fetch_artifacts("test-bucket", _manifest(), client=fake_client)

        fetched = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("Fetched ")
        ]
        assert len(fetched) == 2
        assert fetched[0].endswith("populace_year_2026.h5 (0.0 MB)")
        assert fetched[1].endswith("bl1-aaaa.h5 (0.0 MB)")
