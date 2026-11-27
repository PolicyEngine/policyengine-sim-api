"""Pins of the v2 report app's US cache manifest: entry order, payload and digest.

Every expected value below is what ``main`` produced on 2026-11-25 (commit
6865dc2c) for the fixed inputs in ``fixtures.v2_cache_us_inputs``, with each
planned file stored in a fake bucket as the bytes ``v2-cache-pin:<path>``.
Moving the v2 cache onto the level handlers keeps its manifest (schema
``stage12-mf1``), so each value must stay unchanged.

The manifest records each file's image folder. On 2026-11-27 the folders
moved to the handlers' ``prebuilt/…`` folders, and year files and baselines
began to record theirs too (before, only state files did). That changed the
manifest's digest through the ``folder`` fields alone, so the digest is
pinned twice: without the folder fields
(``test_digest_without_the_image_folders``), unchanged since ``main``, and in
full (``TestImageFolders``), with the value of the new layout.
"""

from __future__ import annotations

import json
from hashlib import sha256

import pytest
from fixtures.v2_cache_us_inputs import (
    STATE_LEVEL_DATASET,
    enable_fixed_spm,
    install_v2_cache_us_inputs,
)
from policyengine_simulation_executor.stage12_cache import keys, precompute
from policyengine_simulation_executor.stage12_cache.models import CacheManifest
from policyengine_simulation_executor.stage12_cache.store import Stage12CacheStore

BUCKET = "bucket-pins"
SPM_SUFFIX = "-spm-7396bf5f4876c42bb6cba0f9533478098edc0861cc3657bd0d60f88dbb26ac39"
YEAR_FILE_2026 = "e4a6dacf5954ccac03abfbcba50c44a3a156d351ed25e3214ae20d7e093e0dac"
FIRST_BASELINE = "88acdd57481da78416475d199be783bb06732a81094dd3658518ad028e0502bd"
ALASKA_2026 = "be2d28af31a275139be0a74a47d02582ca4f6cbfa10d956c3f3c4f227113bbbc"


def _stored_bytes(path: str) -> bytes:
    return f"v2-cache-pin:{path}".encode()


@pytest.fixture(scope="module")
def stored_plan():
    """Plan the fixed US inputs and store every planned file in the fake bucket."""
    with pytest.MonkeyPatch.context() as module_patch:
        inputs = install_v2_cache_us_inputs(module_patch)
        enable_fixed_spm(inputs)
        plan = precompute.plan_artifacts_impl(BUCKET)
        for entry in (*plan.datasets, *plan.baselines, *plan.state_datasets):
            inputs.cache_bucket.store(entry.path, _stored_bytes(entry.path))
        yield plan, inputs


@pytest.fixture(scope="module")
def manifest_payload(stored_plan):
    plan, _inputs = stored_plan
    return precompute.build_manifest(
        plan, Stage12CacheStore(BUCKET)
    ).canonical_payload()


def test_lists_year_files_then_baselines_then_state_files_in_plan_order(
    stored_plan, manifest_payload
):
    plan, _inputs = stored_plan
    artifacts = manifest_payload["artifacts"]

    assert [item["type"] for item in artifacts] == (
        ["dataset"] * 3 + ["baseline"] * 60 + ["state_dataset"] * 153
    )
    assert [item["path"] for item in artifacts] == [
        entry.path for entry in (*plan.datasets, *plan.baselines, *plan.state_datasets)
    ]
    assert [item["identity_digest"] for item in artifacts] == [
        entry.digest
        for entry in (*plan.datasets, *plan.baselines, *plan.state_datasets)
    ]


def test_records_each_stored_files_hash_and_size(manifest_payload):
    for item in manifest_payload["artifacts"]:
        payload = _stored_bytes(item["path"])
        assert item["content_sha256"] == sha256(payload).hexdigest()
        assert item["size_bytes"] == len(payload)


def test_the_top_level_fields(manifest_payload):
    assert {
        key: value for key, value in manifest_payload.items() if key != "artifacts"
    } == {
        "schema": "stage12-mf1",
        "country": "us",
        "years": [2026, 2027, 2025],
        "partition_sha256": (
            "25f8ed5bbcbe25592433b8bda46d73a4597f960f79ae8058f4eef33ed1e0e615"
        ),
        "bundle": {
            "bundle_manifest_sha256": "b" * 64,
            "policyengine_version": "6.20.0",
            "core_package_version": "3.33.3",
            "country_package_version": "2.2.2",
            "data_package_version": "1.4.0",
            "data_artifact_revision": "rev-default",
            "default_dataset": "populace_us_2024",
        },
    }


def test_digest_without_the_image_folders(manifest_payload):
    without_folders = {
        **manifest_payload,
        "artifacts": [
            {key: value for key, value in item.items() if key != "folder"}
            for item in manifest_payload["artifacts"]
        ],
    }

    assert keys.canonical_digest(without_folders) == (
        "d6d8427104e32fa1e1e449f3f7c255485b158cae9fb32e858ad16204881586f8"
    )


def test_the_exact_payload_of_a_one_entry_per_kind_plan(stored_plan):
    plan, _inputs = stored_plan
    small_plan = plan.model_copy(
        update={
            "datasets": plan.datasets[:1],
            "baselines": plan.baselines[:1],
            "state_datasets": plan.state_datasets[:1],
        }
    )

    payload = precompute.build_manifest(
        small_plan, Stage12CacheStore(BUCKET)
    ).canonical_payload()

    assert payload["artifacts"] == [
        {
            "type": "dataset",
            "path": f"datasets/us/{YEAR_FILE_2026}/populace_us_2024_year_2026.h5",
            "filename": "populace_us_2024_year_2026.h5",
            "folder": "prebuilt/us/populace_us_2024/all",
            "year": 2026,
            "identity_digest": YEAR_FILE_2026,
            "content_sha256": (
                "bca9c3dc606ee939620d2d4947eccbc7cbb8eda318480290c5095e0df0417b9b"
            ),
            "size_bytes": 119,
        },
        {
            "type": "baseline",
            "path": (
                f"baselines/us/{FIRST_BASELINE}/"
                f"stage12-bl1-88acdd57481da784{SPM_SUFFIX}.h5"
            ),
            "filename": f"stage12-bl1-88acdd57481da784{SPM_SUFFIX}.h5",
            "folder": "prebuilt/us/populace_us_2024/all",
            "year": 2026,
            "identity_digest": FIRST_BASELINE,
            "content_sha256": (
                "b73d92af8db734a891c932d7f63c6bd6a06a6f777984a4d865551f050adc1e14"
            ),
            "size_bytes": 191,
        },
        {
            "type": "state_dataset",
            "path": (
                f"state-datasets/us/{ALASKA_2026}/{STATE_LEVEL_DATASET}_year_2026.h5"
            ),
            "filename": f"{STATE_LEVEL_DATASET}_year_2026.h5",
            "folder": f"prebuilt/us/{STATE_LEVEL_DATASET}/AK",
            "state_code": "AK",
            "year": 2026,
            "identity_digest": ALASKA_2026,
            "content_sha256": (
                "e162fc40ab4a4407755ac797821fa3f66776a9d5cf46b976c3335f5f9ca92b83"
            ),
            "size_bytes": 135,
        },
    ]
    assert CacheManifest.model_validate(payload).canonical_payload() == payload


def test_publishing_writes_the_canonical_bytes_once_under_their_digest(stored_plan):
    plan, inputs = stored_plan
    record_path = precompute.determinism_record_path(plan.baselines)
    inputs.cache_bucket.store(record_path, b"{}")
    try:
        digest = precompute.publish_manifest_impl(BUCKET, plan)
        manifest_path = f"manifests/{digest}.json"
        published = inputs.cache_bucket.objects[manifest_path]
        republished_digest = precompute.publish_manifest_impl(BUCKET, plan)
    finally:
        inputs.cache_bucket.objects.pop(record_path, None)
        inputs.cache_bucket.metadata.pop(record_path, None)

    payload = precompute.build_manifest(
        plan, Stage12CacheStore(BUCKET)
    ).canonical_payload()
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    assert published == canonical
    # The image download step checks the manifest's SHA-256 against this digest.
    assert sha256(published).hexdigest() == digest
    assert republished_digest == digest
    assert inputs.cache_bucket.objects[manifest_path] == published


def test_the_line_the_precompute_prints_for_the_deploy_job():
    assert precompute.MANIFEST_DIGEST_PREFIX == "STAGE12_CACHE_MANIFEST_DIGEST="
    assert precompute.manifest_digest_line("d" * 64) == (
        "STAGE12_CACHE_MANIFEST_DIGEST=" + "d" * 64
    )


class TestImageFolders:
    """The full digest, which covers each file's image folder.

    It was 0e2b95a1…bce3fe on ``main`` (6865dc2c), with state files in
    ``state-datasets/…`` and no folder for year files and baselines. This is
    the same manifest with every file in its ``prebuilt/…`` folder.
    """

    def test_full_digest(self, manifest_payload):
        assert keys.canonical_digest(manifest_payload) == (
            "eeaaf3bab1c0eeaaa23499129cd59af0d24896e89ad24d167443e37ab01a477e"
        )
