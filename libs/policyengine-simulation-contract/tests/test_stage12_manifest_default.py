"""Tests for adding a Stage 12 v2 worker before the checks and its default after.

A deployment adds the release's worker to the v2 manifest before its checks,
leaving the default version alone, and moves the default after them.
"""

from __future__ import annotations

import pytest
from policyengine_simulation_contract.stage12_manifest import (
    ACTIVE_MANIFEST_KEY,
    PRE_CACHE_MANIFEST_KEY,
    REGION_DATASETS_MANIFEST_KEY,
    V2ManifestLoader,
    add_v2_worker,
    move_v2_default,
    pre_cache_manifest_payload,
    pre_region_datasets_manifest_payload,
    publish_v2_manifest,
    read_v2_manifest,
    write_v2_manifest,
)
from test_stage12_manifest import FakeStore, _worker

OLD = "5.2.0"
NEW = "5.3.0"


def _published(version: str = OLD):
    store = FakeStore()
    publish_v2_manifest(store=store, worker=_worker(version))
    return store


def test_adding_a_newer_worker_keeps_the_default():
    store = _published(OLD)

    manifest = add_v2_worker(current=read_v2_manifest(store), worker=_worker(NEW))

    assert manifest.default_version == OLD
    assert set(manifest.versions) == {OLD, NEW}
    assert manifest.generation == f"{NEW}:{_worker(NEW).application_name}"


def test_the_first_worker_becomes_the_default():
    manifest = add_v2_worker(current=None, worker=_worker(NEW))

    assert manifest.default_version == NEW
    assert set(manifest.versions) == {NEW}


def test_moving_the_default_to_a_newer_added_worker():
    added = add_v2_worker(
        current=read_v2_manifest(_published(OLD)), worker=_worker(NEW)
    )

    moved = move_v2_default(current=added, version=NEW)

    assert moved.default_version == NEW
    assert moved.versions == added.versions
    assert moved.generation == f"default:{NEW}:{_worker(NEW).application_name}"


def test_the_default_moves_to_an_older_worker_only_when_forced():
    added = add_v2_worker(
        current=read_v2_manifest(_published(NEW)), worker=_worker(OLD)
    )

    kept = move_v2_default(current=added, version=OLD)
    forced = move_v2_default(current=added, version=OLD, force_default=True)

    assert kept.default_version == NEW
    assert forced.default_version == OLD


def test_the_default_cannot_move_to_a_worker_that_was_not_added():
    current = read_v2_manifest(_published(OLD))

    with pytest.raises(ValueError, match=f"no v2 worker {NEW!r} to make the default"):
        move_v2_default(current=current, version=NEW)


def test_writing_a_manifest_writes_each_readers_key_and_reads_back():
    store = FakeStore()
    manifest = add_v2_worker(current=None, worker=_worker(NEW))

    write_v2_manifest(store=store, manifest=manifest)

    assert store.writes == [
        (REGION_DATASETS_MANIFEST_KEY, manifest.model_dump(mode="json")),
        (ACTIVE_MANIFEST_KEY, pre_region_datasets_manifest_payload(manifest)),
        (PRE_CACHE_MANIFEST_KEY, pre_cache_manifest_payload(manifest)),
    ]
    assert read_v2_manifest(store) == manifest
    assert V2ManifestLoader(store).resolve()[0] == NEW


def test_an_empty_store_has_no_manifest():
    assert read_v2_manifest(FakeStore()) is None
