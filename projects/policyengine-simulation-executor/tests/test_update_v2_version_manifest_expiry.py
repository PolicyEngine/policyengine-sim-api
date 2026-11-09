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
    V2_MANIFEST_KEYS,
    manifest_digest,
    read_v2_manifest,
)
from test_update_v2_version_manifest import _validation
from test_update_v2_version_manifest_default import (
    OLDER,
    FakeStore,
    ModalNotFoundError,
    _installed_version,
    _run,
    _with_both_workers,
    install_fake_store,
)


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

    assert "--first-publication" in str(refusal.value)
    assert manifest_store.values == {}
    # A refused deployment leaves no empty Dict behind.
    assert manifest_store.opened_with_create_if_missing == [False]


def test_an_operator_starts_a_missing_manifest_with_its_first_worker(
    monkeypatch, tmp_path, manifest_store: FakeStore
):
    _run(
        monkeypatch,
        "--validation-file",
        str(_validation_file(tmp_path)),
        "--first-publication",
    )

    manifest = read_v2_manifest(manifest_store)
    assert manifest is not None
    assert manifest.default_version == _installed_version()
    assert set(manifest.versions) == {_installed_version()}
    assert manifest_store.opened_with_create_if_missing == [True]
    # Only the first: a second start finds the manifest and writes nothing.
    writes = manifest_store.writes
    with pytest.raises(ValueError, match="a v2 manifest exists"):
        _run(
            monkeypatch,
            "--validation-file",
            str(_validation_file(tmp_path)),
            "--first-publication",
        )
    assert manifest_store.writes == writes


def test_a_first_publication_refuses_an_existing_manifest(
    monkeypatch, tmp_path, manifest_store: FakeStore
):
    # It would otherwise move the default to its worker before any check.
    _with_both_workers(manifest_store)
    before = dict(manifest_store.values)
    writes = manifest_store.writes

    with pytest.raises(ValueError, match="a v2 manifest exists"):
        _run(
            monkeypatch,
            "--validation-file",
            str(_validation_file(tmp_path)),
            "--first-publication",
        )

    assert manifest_store.writes == writes
    assert manifest_store.values == before


@pytest.mark.parametrize(
    "arguments",
    [
        ("--first-publication",),
        ("--validation-file", "v.json", "--first-publication", "--keep-default"),
        ("--validation-file", "v.json", "--first-publication", "--force-default"),
    ],
    ids=["without-validation", "with-keep-default", "with-force-default"],
)
def test_a_first_publication_takes_only_a_validation_file(
    monkeypatch, manifest_store: FakeStore, arguments
):
    with pytest.raises(SystemExit) as refusal:
        _run(monkeypatch, *arguments)

    assert refusal.value.code == 2
    assert manifest_store.opened_with_create_if_missing == []


def test_refresh_reads_every_key_and_writes_nothing(
    monkeypatch, manifest_store: FakeStore, capsys
):
    _with_both_workers(manifest_store)
    manifest = read_v2_manifest(manifest_store)
    writes = manifest_store.writes
    manifest_store.reads.clear()

    _run(monkeypatch, "--refresh")

    assert set(manifest_store.reads) >= set(V2_MANIFEST_KEYS)
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


class MissingDict(FakeStore):
    """A Modal environment with no manifest Dict at all, such as a new one."""

    def get(self, key: str, default: object = None) -> object:
        del key, default
        msg = "Dict 'simulation-api-v2-version-manifest' not found"
        raise ModalNotFoundError(msg)


@pytest.mark.parametrize("mode", ["refresh", "deployment"])
def test_a_missing_dict_is_reported_as_a_missing_manifest(monkeypatch, tmp_path, mode):
    # The operator's first recovery step looks for this message, and the
    # deployment's refusal names the way to start the manifest.
    install_fake_store(monkeypatch, MissingDict())
    arguments = {
        "refresh": ("--refresh",),
        "deployment": (
            "--validation-file",
            str(_validation_file(tmp_path)),
            "--keep-default",
        ),
    }[mode]

    with pytest.raises(ValueError, match="there is no v2 manifest") as refusal:
        _run(monkeypatch, *arguments)

    if mode == "deployment":
        assert "--first-publication" in str(refusal.value)
