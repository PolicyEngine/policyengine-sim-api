"""Pins of the US deploy manifest: its entries, its bytes and its digest.

The manifest is content-addressed: its digest names the store object
``manifests/<digest>.json`` and reaches the deploy as
``POLICYENGINE_MANIFEST_DIGEST``, so the same plan must keep giving
byte-identical manifest JSON. Every expected value below is what ``main``
produced on 2026-11-25 (commit 544ff628) for the fixed inputs in
``fixtures.us_prebuilt_inputs``. A deliberate change updates its pinned value
in a commit of its own that says why.
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
from policyengine_simulation_executor.precompute_models import (
    BaselinePlanEntry,
    BundleVersionIdentity,
    DatasetPlanEntry,
    PrecomputePlan,
    StateDatasetPlanEntry,
)

BUCKET = "bucket-pins"

US_MANIFEST_DIGEST = "ae669c63d76697819a43aa1dc428f9de2a22bafb780afe09687c2dc3147cabae"
US_MANIFEST_BYTES_SHA256 = (
    "05d8804f861b83170a232321ed4eabd28b11e3eb9f2e9fd36037b0edbff42e9c"
)
US_SPM_MANIFEST_DIGEST = (
    "b5371811937dba2ac6bbb0cef81e33d34b5658be4165318a0b48a4ee7037d71a"
)
US_SPM_MANIFEST_BYTES_SHA256 = (
    "21231b2ecfd18b27b8010d1d6246341b8a7cc695bbee6292395cf4e823485828"
)

ONE_OF_EACH_MANIFEST_JSON = """\
{
  "artifacts": [
    {
      "digest": "dataset-digest",
      "filename": "populace_cps_year_2026.h5",
      "path": "datasets/us/dataset-digest/populace_cps_year_2026.h5",
      "type": "dataset",
      "year": 2026
    },
    {
      "digest": "baseline-digest",
      "filename": "bl1-0123456789abcdef.h5",
      "path": "baselines/us/baseline-digest/bl1-0123456789abcdef.h5",
      "type": "baseline",
      "year": 2026
    },
    {
      "digest": "state-digest",
      "filename": "populace_cps_local_year_2026.h5",
      "folder": "state-datasets/populace_cps_local/UT",
      "path": "state-datasets/us/state-digest/populace_cps_local_year_2026.h5",
      "state_code": "UT",
      "type": "state_dataset",
      "year": 2026
    }
  ],
  "country": "us",
  "receipt": {
    "data_artifact_revision": "rev-abc",
    "data_version": "1.2.3",
    "default_dataset": "populace_cps",
    "model_version": "9.9.9",
    "policyengine_version": "4.22.0"
  },
  "schema": "mf1"
}"""
ONE_OF_EACH_MANIFEST_DIGEST = (
    "695a0e1780d5034131c0d1b09e09d193335284e1eeaa17fc970bdea6665bfff1"
)


def _one_of_each_plan() -> PrecomputePlan:
    """Return a plan with one dataset, one baseline and one state-level year file."""
    return PrecomputePlan(
        datasets=[
            DatasetPlanEntry(
                year=2026,
                digest="dataset-digest",
                path="datasets/us/dataset-digest/populace_cps_year_2026.h5",
                filename="populace_cps_year_2026.h5",
                exists=True,
            )
        ],
        baselines=[
            BaselinePlanEntry(
                year=2026,
                group=["state/ca"],
                region="region_group/state/ca",
                digest="baseline-digest",
                path="baselines/us/baseline-digest/bl1-0123456789abcdef.h5",
                simulation_id="bl1-0123456789abcdef",
                exists=False,
            )
        ],
        receipt=BundleVersionIdentity(
            policyengine_version="4.22.0",
            model_version="9.9.9",
            data_version="1.2.3",
            data_artifact_revision="rev-abc",
            default_dataset="populace_cps",
        ),
        state_datasets=[
            StateDatasetPlanEntry(
                year=2026,
                state_code="UT",
                region="state/ut",
                dataset="populace_cps_local",
                digest="state-digest",
                path="state-datasets/us/state-digest/populace_cps_local_year_2026.h5",
                filename="populace_cps_local_year_2026.h5",
                folder="state-datasets/populace_cps_local/UT",
                exists=False,
            )
        ],
    )


@pytest.fixture
def us_inputs(monkeypatch):
    return install_us_prebuilt_inputs(monkeypatch)


@pytest.fixture
def us_plan(us_inputs):
    return precompute.plan_artifacts_impl(BUCKET)


@pytest.fixture
def us_spm_plan(monkeypatch, us_inputs):
    enable_fixed_spm_capability(monkeypatch, us_inputs)
    return precompute.plan_artifacts_impl(BUCKET)


class TestManifestOfTheUsPlan:
    def test_lists_datasets_then_baselines_then_state_level_year_files(self, us_plan):
        manifest = precompute.build_manifest(us_plan)

        assert manifest.manifest_schema == "mf1"
        assert manifest.country == "us"
        assert manifest.receipt == us_plan.receipt
        assert [artifact.type for artifact in manifest.artifacts] == (
            ["dataset"] * 3 + ["baseline"] * 60 + ["state_dataset"] * 153
        )
        assert [artifact.path for artifact in manifest.artifacts] == [
            entry.path
            for entry in [
                *us_plan.datasets,
                *us_plan.baselines,
                *us_plan.state_datasets,
            ]
        ]

    def test_manifest_digest(self, us_plan):
        payload = precompute.build_manifest(us_plan).canonical_payload()

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


class TestManifestBytes:
    """The exact text of a small manifest, one entry of each type."""

    def test_published_text(self, us_inputs):
        digest = precompute.publish_manifest_impl(BUCKET, _one_of_each_plan())

        _bucket, path, data, _keyword_arguments = us_inputs.gcs_client.uploads[-1]
        assert data.decode() == ONE_OF_EACH_MANIFEST_JSON
        assert digest == ONE_OF_EACH_MANIFEST_DIGEST
        assert path == f"manifests/{ONE_OF_EACH_MANIFEST_DIGEST}.json"

    def test_digest_output_line(self):
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
