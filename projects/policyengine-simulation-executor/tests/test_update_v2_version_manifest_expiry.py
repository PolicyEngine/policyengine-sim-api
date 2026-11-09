"""Tests for keeping the Stage 12 v2 manifest from expiring, and refusing a missing one.

Modal expires a Dict entry after 7 days without a read or write, and the
entrypoint reads the manifest only for a Stage 12 request. A scheduled
workflow runs ``--refresh`` daily, which reads every key. A deployment's
worker publication (``--keep-default``) refuses a missing manifest, which
it can't tell apart from an expired one; an operator publishes the first
worker with ``--validation-file`` alone.
"""

from __future__ import annotations

import pytest
from policyengine_simulation_contract.stage12_manifest import (
    ACTIVE_MANIFEST_KEY,
    PRE_CACHE_MANIFEST_KEY,
    REGION_DATASETS_MANIFEST_KEY,
    manifest_digest,
    read_v2_manifest,
)
from test_update_v2_version_manifest import _validation
from test_update_v2_version_manifest_default import (
    OLDER,
    FakeStore,
    _installed_version,
    _run,
    _with_both_workers,
    install_fake_store,
)

MANIFEST_KEYS = {
    REGION_DATASETS_MANIFEST_KEY,
    ACTIVE_MANIFEST_KEY,
    PRE_CACHE_MANIFEST_KEY,
}


@pytest.fixture
def manifest_store(monkeypatch) -> FakeStore:
    return install_fake_store(monkeypatch)


def _validation_file(tmp_path):
    validation_file = tmp_path / "validation.json"
    validation_file.write_text(_validation().model_dump_json())
    return validation_file


def test_a_deployment_refuses_to_publish_into_a_missing_manifest(
    monkeypatch, tmp_path, manifest_store: FakeStore
):
    with pytest.raises(ValueError, match="there is no v2 manifest") as refusal:
        _run(
            monkeypatch,
            "--validation-file",
            str(_validation_file(tmp_path)),
            "--keep-default",
        )

    assert "--validation-file without --keep-default" in str(refusal.value)
    assert manifest_store.values == {}


def test_an_operator_publishes_the_first_worker_without_keep_default(
    monkeypatch, tmp_path, manifest_store: FakeStore
):
    _run(monkeypatch, "--validation-file", str(_validation_file(tmp_path)))

    manifest = read_v2_manifest(manifest_store)
    assert manifest is not None
    assert manifest.default_version == _installed_version()


def test_refresh_reads_every_key_and_writes_nothing(
    monkeypatch, manifest_store: FakeStore, capsys
):
    _with_both_workers(manifest_store)
    manifest = read_v2_manifest(manifest_store)
    writes = manifest_store.writes
    manifest_store.reads.clear()

    _run(monkeypatch, "--refresh")

    assert set(manifest_store.reads) >= MANIFEST_KEYS
    assert manifest_store.writes == writes
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"v2 manifest read: default {OLDER}, sha256 {manifest_digest(manifest)}"
    )


def test_refresh_fails_without_a_manifest(monkeypatch, manifest_store: FakeStore):
    with pytest.raises(ValueError, match="there is no v2 manifest"):
        _run(monkeypatch, "--refresh")

    assert manifest_store.values == {}


def test_refresh_never_creates_the_manifest_dict(
    monkeypatch, manifest_store: FakeStore
):
    _with_both_workers(manifest_store)

    _run(monkeypatch, "--refresh")

    assert manifest_store.opened_with_create_if_missing == [False]


@pytest.mark.parametrize(
    "arguments",
    [
        ("--refresh", "--keep-default"),
        ("--refresh", "--force-default"),
        ("--refresh", "--plan-out", "p.json"),
        ("--refresh", "--verify-plan", "p.json"),
    ],
    ids=["keep-default", "force-default", "plan-out", "with-another-mode"],
)
def test_refresh_takes_no_other_option(
    monkeypatch, manifest_store: FakeStore, arguments
):
    with pytest.raises(SystemExit) as refusal:
        _run(monkeypatch, *arguments)

    assert refusal.value.code == 2
    assert manifest_store.reads == []
