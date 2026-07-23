"""Artifact key discipline tests.

The content-addressed store's whole correctness story rests on three
properties locked in here: (1) digests are canonical (field order never
matters), (2) every payload field independently rotates the digest (no
input silently outside the key), and (3) the digests and the upstream
``ScopingStrategy.cache_key`` strings are STABLE — the golden tests turn
any change into a conscious, reviewable diff instead of silent cache
churn (or, worse, a writer/reader mismatch).
"""

import pytest

from fixtures.identity_stubs import install_identity_stubs
from policyengine_simulation_executor import artifact_keys as ak


_DATASET_KWARGS = dict(
    country="us",
    dataset="populace_cps",
    year=2026,
    data_version="1.2.3",
    data_artifact_revision="rev-abc",
    source_sha256="feedbead" * 8,
    data_build_fingerprint="fp-123",
    model_version="9.9.9",
    policyengine_version="4.22.0",
)

_DATASET_GOLDEN = "183ffe49d74669b653fb408b0b7298128ba7c6e990f4479487712a5d79553be3"
_DATASET_NO_SHA_GOLDEN = (
    "25dded8192092f477e4502dae07709e72b6e579f372a9f8346b5eabce07d91b6"
)

_BASELINE_KWARGS = dict(
    country="us",
    region="national",
    scope_key="region_group:row_filter:state_code=CA|row_filter:state_code=WV",
    dataset_digest=_DATASET_GOLDEN,
    model_version="9.9.9",
    policyengine_version="4.22.0",
    # Explicit (== the default) so the perturbation matrix proves the
    # policy field participates in the digest.
    policy=ak.CURRENT_LAW_POLICY,
)

_BASELINE_GOLDEN = "f9cac05d94509895eb48ea33ea5f72d40eb9b954989df05ca6506009d8dedae2"


class TestDigests:
    def test_dataset_key_golden(self):
        assert ak.dataset_key(**_DATASET_KWARGS) == _DATASET_GOLDEN

    def test_none_field_is_a_distinct_identity(self):
        digest = ak.dataset_key(**{**_DATASET_KWARGS, "source_sha256": None})
        assert digest == _DATASET_NO_SHA_GOLDEN
        assert digest != _DATASET_GOLDEN

    def test_baseline_key_golden(self):
        assert ak.baseline_key(**_BASELINE_KWARGS) == _BASELINE_GOLDEN

    @pytest.mark.parametrize("field", sorted(_DATASET_KWARGS))
    def test_every_dataset_field_perturbs_digest(self, field):
        perturbed = {**_DATASET_KWARGS, field: 2027 if field == "year" else "other"}
        assert ak.dataset_key(**perturbed) != _DATASET_GOLDEN

    @pytest.mark.parametrize("field", sorted(_BASELINE_KWARGS))
    def test_every_baseline_field_perturbs_digest(self, field):
        perturbed = {**_BASELINE_KWARGS, field: "other"}
        assert ak.baseline_key(**perturbed) != _BASELINE_GOLDEN

    def test_canonical_digest_ignores_insertion_order(self):
        forward = {"a": 1, "b": {"c": 2, "d": None}}
        backward = {"b": {"d": None, "c": 2}, "a": 1}
        assert ak.canonical_digest(forward) == ak.canonical_digest(backward)

    def test_simulation_id_shape(self):
        sim_id = ak.baseline_simulation_id(_BASELINE_GOLDEN)
        assert sim_id == "bl1-f9cac05d94509895"
        # Filename-safe and disjoint from the dataset filename pattern.
        assert "/" not in sim_id
        assert "_year_" not in sim_id


class TestStoreLayout:
    def test_dataset_path(self):
        path = ak.dataset_artifact_path("US", "d" * 64, "populace_cps", 2026)
        assert path == f"datasets/us/{'d' * 64}/populace_cps_year_2026.h5"

    def test_baseline_path(self):
        path = ak.baseline_artifact_path("us", "b" * 64, "bl1-abc")
        assert path == f"baselines/us/{'b' * 64}/bl1-abc.h5"

    def test_manifest_and_marker_paths(self):
        assert ak.manifest_path("m" * 64) == f"manifests/{'m' * 64}.json"
        assert ak.deployed_marker_path("beta") == "deployed/beta.json"


class TestUpstreamCacheKeyGoldens:
    """Golden tests on policyengine's ScopingStrategy.cache_key format.

    The baseline key embeds these strings verbatim. If a policyengine pin
    bump breaks one of these tests, the upstream format changed: every
    baseline key rotates (correct but a full recompute) — flag it in the
    bump PR rather than discovering it as a silent 100%-miss deploy.
    """

    def test_row_filter_cache_key_golden(self):
        from policyengine.core.scoping_strategy import RowFilterStrategy

        strategy = RowFilterStrategy(variable_name="state_code", variable_value="CA")
        assert strategy.cache_key == "row_filter:state_code=CA"

    def test_region_group_cache_key_golden_and_order_invariance(self):
        from policyengine.core.scoping_strategy import (
            RegionGroupStrategy,
            RowFilterStrategy,
        )

        ca = RowFilterStrategy(variable_name="state_code", variable_value="CA")
        wv = RowFilterStrategy(variable_name="state_code", variable_value="WV")
        expected = "region_group:row_filter:state_code=CA|row_filter:state_code=WV"
        assert RegionGroupStrategy(members=[wv, ca]).cache_key == expected
        assert RegionGroupStrategy(members=[ca, wv]).cache_key == expected


@pytest.fixture
def stub_identity_sources(monkeypatch):
    """Stub the bundle/manifest/receipt seams collect_dataset_identity reads."""
    return install_identity_stubs(monkeypatch)


class TestIdentityCollection:
    def test_collected_identity_matches_golden(self, stub_identity_sources):
        identity = ak.collect_dataset_identity("us", 2026)
        assert identity.digest == _DATASET_GOLDEN
        assert identity.filename == "populace_cps_year_2026.h5"
        assert identity.store_path == (
            f"datasets/us/{_DATASET_GOLDEN}/populace_cps_year_2026.h5"
        )

    def test_receipt_version_mismatch_drops_source_sha(self, stub_identity_sources):
        stub_identity_sources.receipt_entry = {
            "country": "us",
            "version": "0.0.1",
            "installed_sha256": "feedbead" * 8,
        }
        identity = ak.collect_dataset_identity("us", 2026)
        assert identity.source_sha256 is None
        assert identity.digest == _DATASET_NO_SHA_GOLDEN

    def test_missing_receipt_falls_back_to_none(self, stub_identity_sources):
        stub_identity_sources.receipt_entry = None
        identity = ak.collect_dataset_identity("us", 2026)
        assert identity.source_sha256 is None
        assert identity.digest == _DATASET_NO_SHA_GOLDEN

    def test_expected_sha_used_when_installed_absent(self, stub_identity_sources):
        stub_identity_sources.receipt_entry = {
            "country": "us",
            "version": "1.2.3",
            "expected_sha256": "feedbead" * 8,
        }
        assert ak.collect_dataset_identity("us", 2026).digest == _DATASET_GOLDEN

    def test_baseline_identity_composes(self, stub_identity_sources):
        identity = ak.collect_baseline_identity(
            "us",
            2026,
            region="national",
            scope_key=_BASELINE_KWARGS["scope_key"],
        )
        assert identity.digest == _BASELINE_GOLDEN
        assert identity.simulation_id == "bl1-f9cac05d94509895"
        assert identity.store_path == (
            f"baselines/us/{_BASELINE_GOLDEN}/bl1-f9cac05d94509895.h5"
        )
