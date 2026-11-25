"""Pins of the US deploy manifest: its entries, its bytes and its digest.

The manifest is content-addressed: its digest names the store object
``manifests/<digest>.json`` and reaches the deploy as
``POLICYENGINE_MANIFEST_DIGEST``, so the same plan must keep giving
byte-identical manifest JSON. The approved 1.6 · 1 design (section 4)
replaced the ``mf1`` manifest with one ``mf2`` manifest per country, whose
entries share one shape and record each file's SHA-256 and size, so the
order, bytes and digests below were recorded from the new code for the fixed
inputs in ``fixtures.us_prebuilt_inputs``. Write-once publishing, the
``MANIFEST_DIGEST=`` output line and the deployed marker are unchanged from
``main`` before the handlers (commit 544ff628).
"""

from __future__ import annotations

import hashlib

import pytest
from fixtures.us_prebuilt_inputs import (
    enable_fixed_spm_capability,
    install_us_prebuilt_inputs,
)
from policyengine_simulation_executor import artifact_keys, precompute
from policyengine_simulation_executor.artifact_store import ArtifactStore

BUCKET = "bucket-pins"

US_MANIFEST_DIGEST = "1461437612f03df403013e98fa10dfd65609ae828f31a79bf7adc899513a6d57"
US_MANIFEST_BYTES_SHA256 = (
    "0713954874042dfa47bc7a9cd717f0e174c78128e57d5b13c3c83afb37cca8e9"
)
US_SPM_MANIFEST_DIGEST = (
    "fced66e5754bd8bfae462e806d5215234eedc1ad69cfb78eba1e774c92b9d0b0"
)
US_SPM_MANIFEST_BYTES_SHA256 = (
    "dce59ab37a9b46a91cea3fee94980efba2f865ce3c3b634a95ad7d63480affb2"
)


def _store_every_planned_file(inputs, plan):
    """Store each planned file, its path as its bytes, with its hash and size."""
    for entry in [*plan.year_files, *plan.baselines]:
        content = entry.path.encode()
        inputs.gcs_client.objects[entry.path] = content
        inputs.gcs_client.object_metadata[entry.path] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": str(len(content)),
        }


@pytest.fixture
def us_inputs(monkeypatch):
    return install_us_prebuilt_inputs(monkeypatch)


@pytest.fixture
def us_plan(us_inputs):
    plan = precompute.plan_artifacts_impl(BUCKET, "us")
    _store_every_planned_file(us_inputs, plan)
    return plan


@pytest.fixture
def us_spm_plan(monkeypatch, us_inputs):
    enable_fixed_spm_capability(monkeypatch, us_inputs)
    plan = precompute.plan_artifacts_impl(BUCKET, "us")
    _store_every_planned_file(us_inputs, plan)
    return plan


def _stored(inputs, plan):
    store = ArtifactStore(BUCKET)
    return {
        entry.path: store.describe(entry.path)
        for entry in [*plan.year_files, *plan.baselines]
    }


class TestManifestOfTheUsPlan:
    def test_lists_each_years_files_then_the_baselines(self, us_inputs, us_plan):
        manifest = precompute.build_manifest(us_plan, _stored(us_inputs, us_plan))

        assert manifest.manifest_schema == "mf2"
        assert manifest.country == "us"
        assert manifest.receipt == us_plan.receipt
        assert [
            (artifact.kind, artifact.level, artifact.year)
            for artifact in manifest.artifacts
        ] == [
            *[
                (kind, level, year)
                for year in (2026, 2027, 2025)
                for kind, level in [("year_file", "national")]
                + [("year_file", "state")] * 51
            ],
            *[
                ("baseline", "state_group", year)
                for year in (2026, 2027, 2025)
                for _group in range(20)
            ],
        ]
        assert [artifact.path for artifact in manifest.artifacts] == [
            entry.path for entry in [*us_plan.year_files, *us_plan.baselines]
        ]

    def test_manifest_digest(self, us_inputs, us_plan):
        payload = precompute.build_manifest(
            us_plan, _stored(us_inputs, us_plan)
        ).canonical_payload()

        assert artifact_keys.canonical_digest(payload) == US_MANIFEST_DIGEST

    def test_published_manifest_bytes_and_digest(self, us_inputs, us_plan):
        digest = precompute.publish_manifest_impl(BUCKET, us_plan)

        assert digest == US_MANIFEST_DIGEST
        bucket, path, data, keyword_arguments = us_inputs.gcs_client.uploads[-1]
        assert bucket == BUCKET
        assert path == f"manifests/{US_MANIFEST_DIGEST}.json"
        assert keyword_arguments == {
            "content_type": "application/json",
            "if_generation_match": 0,
        }
        assert hashlib.sha256(data).hexdigest() == US_MANIFEST_BYTES_SHA256

    def test_spm_manifest_bytes_and_digest(self, us_inputs, us_spm_plan):
        digest = precompute.publish_manifest_impl(BUCKET, us_spm_plan)

        assert digest == US_SPM_MANIFEST_DIGEST
        _bucket, path, data, _keyword_arguments = us_inputs.gcs_client.uploads[-1]
        assert path == f"manifests/{US_SPM_MANIFEST_DIGEST}.json"
        assert hashlib.sha256(data).hexdigest() == US_SPM_MANIFEST_BYTES_SHA256

    def test_republishing_keeps_the_stored_manifest(self, us_inputs, us_plan):
        first_digest = precompute.publish_manifest_impl(BUCKET, us_plan)
        stored = dict(us_inputs.gcs_client.objects)

        assert precompute.publish_manifest_impl(BUCKET, us_plan) == first_digest
        assert us_inputs.gcs_client.objects == stored


def test_digest_output_line():
    assert precompute.MANIFEST_DIGEST_PREFIX == "MANIFEST_DIGEST="
    assert (
        precompute.manifest_digest_line(US_MANIFEST_DIGEST)
        == f"MANIFEST_DIGEST={US_MANIFEST_DIGEST}"
    )


class TestDeployedMarker:
    def test_marker_overwrites_its_environment_path(self, us_inputs):
        store = ArtifactStore(BUCKET)

        store.write_deployed_marker("prod", {"manifest_digest": "first"})
        store.write_deployed_marker("prod", {"manifest_digest": US_MANIFEST_DIGEST})

        uploads = us_inputs.gcs_client.uploads
        assert [(bucket, path) for bucket, path, _data, _kwargs in uploads] == [
            (BUCKET, "deployed/prod.json"),
            (BUCKET, "deployed/prod.json"),
        ]
        assert uploads[-1][3] == {"content_type": "application/json"}
        assert us_inputs.gcs_client.objects["deployed/prod.json"] == (
            f'{{\n  "manifest_digest": "{US_MANIFEST_DIGEST}"\n}}'.encode()
        )
