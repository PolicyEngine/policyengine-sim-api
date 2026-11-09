"""Tests for publishing a Stage 12 v2 worker before the checks and its default after.

The deployment adds the release's worker with ``--keep-default`` before its
checks, then moves the default with ``--move-default`` after them, checks it
with ``--verify-plan`` and, if a later step fails, restores it with
``--restore-plan``.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from policyengine_simulation_contract.stage12_manifest import (
    ACTIVE_MANIFEST_KEY,
    PRE_CACHE_MANIFEST_KEY,
    REGION_DATASETS_MANIFEST_KEY,
    V2ManifestLoader,
    V2VersionManifest,
    V2WorkerVersion,
    add_v2_worker,
    move_v2_default,
    read_v2_manifest,
    v2_application_name,
    v2_manifest_copies,
    write_v2_manifest,
)
from src.modal.utils import update_v2_version_manifest as publisher
from test_update_v2_version_manifest import _validation

OLDER = "1.0.0"


class FakeStore:
    """A Modal Dict stand-in that can fail on a chosen later write."""

    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.writes = 0
        self.writes_until_failure: int | None = None

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def __setitem__(self, key: str, value: object) -> None:
        if self.writes_until_failure == 0:
            self.writes_until_failure = None
            msg = "Modal Dict write failed"
            raise RuntimeError(msg)
        if self.writes_until_failure is not None:
            self.writes_until_failure -= 1
        self.writes += 1
        self.values[key] = json.loads(json.dumps(value))

    def holds(self, manifest: object) -> bool:
        copies = v2_manifest_copies(V2VersionManifest.model_validate(manifest))
        return all(self.values.get(key) == copy for key, copy in copies.items())


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()

    def from_name(name, *, environment_name, create_if_missing):
        del name, environment_name, create_if_missing
        return fake

    monkeypatch.setattr(
        publisher, "modal", SimpleNamespace(Dict=SimpleNamespace(from_name=from_name))
    )
    return fake


def _installed_worker() -> V2WorkerVersion:
    return publisher.build_worker(_validation().model_dump(mode="json"))


def _installed_version() -> str:
    return _installed_worker().bundle.policyengine_version


def _older_worker() -> V2WorkerVersion:
    payload = _installed_worker().model_dump(mode="json")
    name = v2_application_name(OLDER)
    payload["application_name"] = name
    payload["validation"]["application_name"] = name
    payload["bundle"]["policyengine_version"] = OLDER
    payload["bundle"]["policyengine_requirement"] = f"policyengine=={OLDER}"
    return V2WorkerVersion.model_validate(payload)


def _with_older_default(store: FakeStore) -> None:
    write_v2_manifest(
        store=store, manifest=add_v2_worker(current=None, worker=_older_worker())
    )


def _with_both_workers(store: FakeStore) -> None:
    _with_older_default(store)
    write_v2_manifest(
        store=store,
        manifest=add_v2_worker(
            current=read_v2_manifest(store), worker=_installed_worker()
        ),
    )


def _run(monkeypatch, *arguments: str) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["update_v2_version_manifest", "--environment", "staging", *arguments],
    )
    publisher.main()


def _move(monkeypatch, tmp_path, version: str) -> dict:
    plan_file = tmp_path / "plan.json"
    _run(monkeypatch, "--move-default", version, "--plan-out", str(plan_file))
    return json.loads(plan_file.read_text())


def test_keep_default_adds_the_worker_and_leaves_the_default(
    monkeypatch, tmp_path, store
):
    _with_older_default(store)
    validation_file = tmp_path / "validation.json"
    validation_file.write_text(_validation().model_dump_json())

    _run(monkeypatch, "--validation-file", str(validation_file), "--keep-default")

    manifest = read_v2_manifest(store)
    assert manifest is not None
    assert manifest.default_version == OLDER
    assert set(manifest.versions) == {OLDER, _installed_version()}


def test_move_default_plans_moves_and_reports_it(monkeypatch, tmp_path, store, capsys):
    _with_both_workers(store)
    before = read_v2_manifest(store)

    plan = _move(monkeypatch, tmp_path, _installed_version())

    assert plan["version"] == _installed_version()
    assert plan["before"]["default_version"] == OLDER
    assert plan["after"]["default_version"] == _installed_version()
    assert plan["release_is_default"] is True
    assert plan["before"] == json.loads(before.model_dump_json())
    assert V2ManifestLoader(store).resolve()[0] == _installed_version()
    assert capsys.readouterr().out.splitlines()[-3:] == [
        f"before_sha256={plan['before_sha256']}",
        f"after_sha256={plan['after_sha256']}",
        "release_is_default=true",
    ]


def test_move_default_keeps_a_newer_default(monkeypatch, tmp_path, store):
    # The default is the installed worker, newer than the release being moved
    # to, so the default stays.
    _with_both_workers(store)
    write_v2_manifest(
        store=store,
        manifest=move_v2_default(
            current=read_v2_manifest(store), version=_installed_version()
        ),
    )

    writes = store.writes

    plan = _move(monkeypatch, tmp_path, OLDER)

    assert plan["release_is_default"] is False
    assert plan["before"] == plan["after"]
    assert store.writes == writes
    assert read_v2_manifest(store).default_version == _installed_version()


def test_move_default_refuses_a_version_without_a_worker(monkeypatch, tmp_path, store):
    _with_older_default(store)

    with pytest.raises(ValueError, match="no v2 worker"):
        _move(monkeypatch, tmp_path, _installed_version())
    assert read_v2_manifest(store).default_version == OLDER
    assert not (tmp_path / "plan.json").exists()


def test_move_default_refuses_an_empty_manifest(monkeypatch, tmp_path, store):
    with pytest.raises(ValueError, match="no v2 manifest"):
        _move(monkeypatch, tmp_path, _installed_version())


def test_verify_accepts_the_moved_manifest(monkeypatch, tmp_path, store, capsys):
    _with_both_workers(store)
    _move(monkeypatch, tmp_path, _installed_version())

    _run(monkeypatch, "--verify-plan", str(tmp_path / "plan.json"))

    assert f"v2 default version: {_installed_version()}" in capsys.readouterr().out


def test_verify_refuses_a_manifest_changed_since_the_move(monkeypatch, tmp_path, store):
    _with_both_workers(store)
    plan = _move(monkeypatch, tmp_path, _installed_version())
    write_v2_manifest(
        store=store,
        manifest=add_v2_worker(current=plan["before"], worker=_installed_worker()),
    )

    with pytest.raises(ValueError, match="is not what this default move wrote"):
        _run(monkeypatch, "--verify-plan", str(tmp_path / "plan.json"))


def test_restore_puts_the_default_back_only_while_the_move_is_live(
    monkeypatch, tmp_path, store, capsys
):
    _with_both_workers(store)
    plan = _move(monkeypatch, tmp_path, _installed_version())
    plan_file = str(tmp_path / "plan.json")

    _run(monkeypatch, "--restore-plan", plan_file)
    _run(monkeypatch, "--restore-plan", plan_file)

    output = capsys.readouterr().out
    assert "v2 default restore: restored" in output
    assert "v2 default restore: unchanged" in output
    assert json.loads(read_v2_manifest(store).model_dump_json()) == plan["before"]


def test_restore_refuses_a_manifest_another_publish_changed(
    monkeypatch, tmp_path, store
):
    _with_both_workers(store)
    plan = _move(monkeypatch, tmp_path, _installed_version())
    changed = add_v2_worker(current=plan["after"], worker=_older_worker())
    write_v2_manifest(store=store, manifest=changed)

    with pytest.raises(ValueError, match="nothing was restored"):
        _run(monkeypatch, "--restore-plan", str(tmp_path / "plan.json"))
    assert read_v2_manifest(store) == changed


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("--keep-default",),
        ("--move-default", "1.0.0"),
        ("--move-default", "1.0.0", "--plan-out", "p.json", "--verify-plan", "p.json"),
        ("--verify-plan", "p.json", "--restore-plan", "p.json"),
    ],
    ids=[
        "no-mode",
        "keep-default-without-validation",
        "move-without-plan-file",
        "two-modes",
        "verify-and-restore",
    ],
)
def test_the_command_line_refuses_an_incomplete_or_mixed_request(
    monkeypatch, store, arguments
):
    with pytest.raises(SystemExit) as refusal:
        _run(monkeypatch, *arguments)

    assert refusal.value.code == 2
    assert store.values == {}


def test_the_plan_is_kept_before_the_manifest_is_written(monkeypatch, tmp_path, store):
    _with_both_workers(store)
    store.writes_until_failure = 0

    with pytest.raises(RuntimeError, match="write failed"):
        _move(monkeypatch, tmp_path, _installed_version())

    assert json.loads((tmp_path / "plan.json").read_text())["release_is_default"]


@pytest.mark.parametrize("completed_writes", [1, 2], ids=["one-key", "two-keys"])
def test_restore_recovers_from_a_move_that_stopped_part_way(
    monkeypatch, tmp_path, store, capsys, completed_writes
):
    # The move wrote only some of the manifest's keys, so readers fall back
    # to an older key's copy, which a digest of either plan state misses.
    _with_both_workers(store)
    store.writes_until_failure = completed_writes
    with pytest.raises(RuntimeError, match="write failed"):
        _move(monkeypatch, tmp_path, _installed_version())
    plan = json.loads((tmp_path / "plan.json").read_text())

    _run(monkeypatch, "--restore-plan", str(tmp_path / "plan.json"))

    assert "v2 default restore: restored" in capsys.readouterr().out
    assert store.holds(plan["before"])


def test_a_restore_that_stopped_part_way_can_be_run_again(monkeypatch, tmp_path, store):
    _with_both_workers(store)
    plan = _move(monkeypatch, tmp_path, _installed_version())
    store.writes_until_failure = 1
    with pytest.raises(RuntimeError, match="write failed"):
        _run(monkeypatch, "--restore-plan", str(tmp_path / "plan.json"))

    _run(monkeypatch, "--restore-plan", str(tmp_path / "plan.json"))

    assert store.holds(plan["before"])


def test_move_refuses_a_manifest_whose_copies_differ(monkeypatch, tmp_path, store):
    # Moving from a mixed store would write one copy's content to every key,
    # dropping what the others hold (such as region datasets).
    _with_both_workers(store)
    older_copy = v2_manifest_copies(
        add_v2_worker(current=None, worker=_older_worker())
    )[PRE_CACHE_MANIFEST_KEY]
    store.values[PRE_CACHE_MANIFEST_KEY] = older_copy
    writes = store.writes

    with pytest.raises(ValueError, match="copies of the v2 manifest differ"):
        _move(monkeypatch, tmp_path, _installed_version())
    assert store.writes == writes


def test_verify_checks_the_copy_older_readers_read(monkeypatch, tmp_path, store):
    _with_both_workers(store)
    plan = _move(monkeypatch, tmp_path, _installed_version())
    before_copy = v2_manifest_copies(V2VersionManifest.model_validate(plan["before"]))
    store.values[PRE_CACHE_MANIFEST_KEY] = before_copy[PRE_CACHE_MANIFEST_KEY]

    with pytest.raises(ValueError, match="is not what this default move wrote"):
        _run(monkeypatch, "--verify-plan", str(tmp_path / "plan.json"))


@pytest.mark.parametrize(
    "mode",
    [
        ("--move-default", "1.0.0", "--plan-out", "p.json"),
        ("--verify-plan", "p.json"),
        ("--restore-plan", "p.json"),
    ],
    ids=["move", "verify", "restore"],
)
def test_force_default_is_refused_outside_a_worker_publication(
    monkeypatch, store, mode
):
    with pytest.raises(SystemExit) as refusal:
        _run(monkeypatch, *mode, "--force-default")

    assert refusal.value.code == 2
    assert store.values == {}


# Modal expires a Dict entry after 7 days without a read or write, and current
# readers never read the oldest key, so a delayed rerun or an operator's
# restore can find an older key missing. A write never deletes a key.
OLDER_KEYS = pytest.mark.parametrize(
    "expired_key",
    [ACTIVE_MANIFEST_KEY, PRE_CACHE_MANIFEST_KEY],
    ids=["active-with-cache", "active"],
)


@OLDER_KEYS
def test_move_rewrites_an_expired_older_key(monkeypatch, tmp_path, store, expired_key):
    _with_both_workers(store)
    del store.values[expired_key]

    plan = _move(monkeypatch, tmp_path, _installed_version())

    assert store.holds(plan["after"])


@OLDER_KEYS
def test_restore_rewrites_an_expired_older_key(
    monkeypatch, tmp_path, store, capsys, expired_key
):
    _with_both_workers(store)
    plan = _move(monkeypatch, tmp_path, _installed_version())
    del store.values[expired_key]

    _run(monkeypatch, "--restore-plan", str(tmp_path / "plan.json"))

    assert "v2 default restore: restored" in capsys.readouterr().out
    assert store.holds(plan["before"])


@OLDER_KEYS
def test_verify_accepts_an_expired_older_key(
    monkeypatch, tmp_path, store, capsys, expired_key
):
    _with_both_workers(store)
    _move(monkeypatch, tmp_path, _installed_version())
    del store.values[expired_key]

    _run(monkeypatch, "--verify-plan", str(tmp_path / "plan.json"))

    assert f"v2 default version: {_installed_version()}" in capsys.readouterr().out


def test_move_refuses_a_manifest_without_its_newest_key(monkeypatch, tmp_path, store):
    # Current readers read the newest key, so it is required.
    _with_both_workers(store)
    del store.values[REGION_DATASETS_MANIFEST_KEY]
    writes = store.writes

    with pytest.raises(ValueError, match="copies of the v2 manifest differ"):
        _move(monkeypatch, tmp_path, _installed_version())
    assert store.writes == writes


def test_restore_refuses_another_manifest_under_any_one_key(
    monkeypatch, tmp_path, store
):
    _with_both_workers(store)
    plan = _move(monkeypatch, tmp_path, _installed_version())
    other = add_v2_worker(current=None, worker=_older_worker())
    store.values[ACTIVE_MANIFEST_KEY] = v2_manifest_copies(other)[ACTIVE_MANIFEST_KEY]
    writes = store.writes

    with pytest.raises(ValueError, match="nothing was restored"):
        _run(monkeypatch, "--restore-plan", str(tmp_path / "plan.json"))
    assert store.writes == writes
    assert (
        store.values[REGION_DATASETS_MANIFEST_KEY]
        == (
            v2_manifest_copies(V2VersionManifest.model_validate(plan["after"]))[
                REGION_DATASETS_MANIFEST_KEY
            ]
        )
    )


@pytest.mark.parametrize("mode", ["--verify-plan", "--restore-plan"])
def test_a_plan_whose_manifests_were_altered_is_refused(
    monkeypatch, tmp_path, store, mode
):
    _with_both_workers(store)
    plan = _move(monkeypatch, tmp_path, _installed_version())
    altered = dict(plan, after=plan["before"])
    (tmp_path / "plan.json").write_text(json.dumps(altered))
    writes = store.writes

    with pytest.raises(ValueError, match="do not match its digests"):
        _run(monkeypatch, mode, str(tmp_path / "plan.json"))
    assert store.writes == writes
