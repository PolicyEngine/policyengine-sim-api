"""The internal routes that answer ``/versions`` from the v2 version manifest.

``GET /internal/v2/versions`` and ``GET /internal/v2/versions/{kind}`` give
the answers ``/versions`` would give from the v2 version manifest, so a
staging deploy can compare them with the old gateway's answers while
``/versions`` still forwards to it.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from conftest import FakeBackend, make_settings
from fastapi import HTTPException
from fastapi.testclient import TestClient
from policyengine_simulation_contract.stage12_manifest import (
    V2_VERSION_MANIFEST_NAME,
    publish_v2_manifest,
)
from policyengine_simulation_entry import app as app_module
from policyengine_simulation_entry import v2_versions
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.v2_versions import (
    ModalV2VersionsSource,
    version_map_from_manifest,
    versions_from_manifest,
)
from v2_versions_support import TWO_RELEASES, manifest_worker

UNAVAILABLE_DETAIL = "The v2 version manifest is unavailable."


class FakeVersionsSource:
    """A v2 versions source that returns one manifest, or raises ``error``."""

    def __init__(self, manifest=TWO_RELEASES, error: BaseException | None = None):
        self.manifest = manifest
        self.error = error
        self.loads = 0

    async def load_manifest(self):
        self.loads += 1
        if self.error is not None:
            raise self.error
        return self.manifest


def versions_client(source, *, auth_dependency=lambda: None):
    """Return a test client whose app reads the v2 manifest from ``source``."""
    backend = FakeBackend()
    app = create_app(
        settings=make_settings(),
        backend=backend,
        auth_dependency=auth_dependency,
        v2_versions_source=source,
    )
    return TestClient(app), backend


def test_the_whole_answer_comes_from_the_manifest():
    source = FakeVersionsSource()
    client, backend = versions_client(source)

    with client:
        result = client.get("/internal/v2/versions")

    assert result.status_code == 200
    assert result.json() == versions_from_manifest(TWO_RELEASES).model_dump(mode="json")
    # Fields the answer leaves empty are sent as null, as the gateway sends them.
    assert result.json()["bundles"]["6.2.7"]["us"]["spm"] is None
    assert backend.requests == []


@pytest.mark.parametrize(
    ("kind", "expected_kind"),
    [("policyengine", "policyengine"), ("us", "us"), ("UK", "uk")],
)
def test_each_kind_answer_comes_from_the_manifest(kind, expected_kind):
    client, backend = versions_client(FakeVersionsSource())

    with client:
        result = client.get(f"/internal/v2/versions/{kind}")

    assert result.status_code == 200
    assert result.json() == version_map_from_manifest(
        TWO_RELEASES, expected_kind
    ).model_dump(mode="json")
    assert backend.requests == []


def test_an_unknown_kind_is_404_with_the_gateways_detail_before_any_read():
    source = FakeVersionsSource()
    client, _ = versions_client(source)

    with client:
        result = client.get("/internal/v2/versions/ca")

    assert result.status_code == 404
    assert result.json() == {"detail": "Unknown version kind: ca"}
    assert source.loads == 0


@pytest.mark.parametrize(
    "error",
    [
        ValueError("no valid v2 version manifest is available"),
        RuntimeError("modal unavailable"),
        TimeoutError(),
    ],
)
@pytest.mark.parametrize("path", ["/internal/v2/versions", "/internal/v2/versions/us"])
def test_an_unreadable_manifest_is_503(error, path):
    client, _ = versions_client(FakeVersionsSource(error=error))

    with client:
        result = client.get(path)

    assert result.status_code == 503
    assert result.json() == {"detail": UNAVAILABLE_DETAIL}
    assert result.headers["retry-after"] == "10"


@pytest.mark.parametrize("path", ["/internal/v2/versions", "/internal/v2/versions/uk"])
def test_without_a_configured_manifest_the_routes_are_503(path):
    client, _ = versions_client(None)

    with client:
        result = client.get(path)

    assert result.status_code == 503
    assert result.json() == {"detail": UNAVAILABLE_DETAIL}


def test_a_manifest_read_that_does_not_finish_is_503(monkeypatch):
    class StalledVersionsSource(FakeVersionsSource):
        async def load_manifest(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(app_module, "V2_MANIFEST_READ_TIMEOUT_SECONDS", 0.001)
    client, _ = versions_client(StalledVersionsSource())

    with client:
        result = client.get("/internal/v2/versions")

    assert result.status_code == 503
    assert result.json() == {"detail": UNAVAILABLE_DETAIL}


def test_the_routes_are_public_like_versions():
    def reject_every_caller():
        raise HTTPException(status_code=403, detail="Not authenticated")

    client, _ = versions_client(
        FakeVersionsSource(), auth_dependency=reject_every_caller
    )

    with client:
        whole = client.get("/internal/v2/versions")
        kind = client.get("/internal/v2/versions/us")

    assert whole.status_code == 200
    assert kind.status_code == 200


def test_the_routes_are_not_in_the_openapi_document():
    client, _ = versions_client(FakeVersionsSource())

    with client:
        paths = client.get("/openapi.json").json()["paths"]

    assert not [path for path in paths if path.startswith("/internal/v2")]


def test_the_modal_source_reads_the_published_manifest():
    store: dict[str, object] = {}
    worker_version = manifest_worker("6.2.7", us_version="1.700.0", uk_version="2.1.0")
    published = publish_v2_manifest(store=store, worker=worker_version)

    manifest = asyncio.run(ModalV2VersionsSource(store).load_manifest())

    assert manifest == published


def test_the_modal_source_raises_when_no_manifest_was_published():
    with pytest.raises(ValueError, match="no valid v2 version manifest"):
        asyncio.run(ModalV2VersionsSource({}).load_manifest())


def test_the_modal_source_opens_the_configured_manifest_dict(monkeypatch):
    opened = []

    def open_dict(name, *, environment_name):
        opened.append((name, environment_name))
        return {}

    monkeypatch.setattr(v2_versions.modal.Dict, "from_name", open_dict)

    source = ModalV2VersionsSource.from_settings(
        make_settings(stage12_v2_manifest_environment="staging")
    )

    assert isinstance(source, ModalV2VersionsSource)
    assert opened == [(V2_VERSION_MANIFEST_NAME, "staging")]


def test_no_modal_source_without_a_manifest_environment(monkeypatch):
    def refuse_to_open(*_args, **_kwargs):
        msg = "no manifest environment is configured"
        raise AssertionError(msg)

    monkeypatch.setattr(v2_versions.modal.Dict, "from_name", refuse_to_open)

    assert ModalV2VersionsSource.from_settings(make_settings()) is None


def test_the_app_builds_the_modal_source_from_its_settings(monkeypatch):
    opened = []

    def open_dict(name, *, environment_name):
        opened.append((name, environment_name))
        return {}

    monkeypatch.setattr(v2_versions.modal.Dict, "from_name", open_dict)

    # The deploy sets the manifest environment together with the other Stage
    # 12 resources, which settings validation requires.
    app = create_app(
        settings=make_settings(
            stage12_v2_manifest_environment="main",
            stage12_database_url=(
                "postgresql://policyengine_v2_runtime:secret@"
                "test.pooler.supabase.com:5432/postgres?sslmode=require"
            ),
            stage12_artifact_bucket="policyengine-stage12-production",
        ),
        backend=FakeBackend(),
        auth_dependency=lambda: None,
        # A stand-in, so only the version source opens a manifest Dict here.
        comparison_backend=object(),
    )
    with TestClient(app) as client:
        result = client.get("/internal/v2/versions")

    assert opened == [(V2_VERSION_MANIFEST_NAME, "main")]
    # The opened store is empty, so no manifest has been published to it.
    assert result.status_code == 503


def test_the_modal_source_reads_on_its_own_thread():
    # A stalled Modal read must not hold threads from the event loop's default
    # executor, which the Stage 12 comparison path uses.
    reading_threads = []

    class RecordingStore(dict):
        def get(self, key, default=None):
            reading_threads.append(threading.current_thread().name)
            return super().get(key, default)

    store = RecordingStore()
    publish_v2_manifest(
        store=store,
        worker=manifest_worker("6.2.7", us_version="1.700.0", uk_version="2.1.0"),
    )
    reading_threads.clear()

    asyncio.run(ModalV2VersionsSource(store).load_manifest())

    assert reading_threads
    assert all(name.startswith("v2-version-manifest") for name in reading_threads)
