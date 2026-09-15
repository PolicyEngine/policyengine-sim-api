"""Artifact key discipline tests.

The content-addressed store's whole correctness story rests on three
properties locked in here: (1) digests are canonical (field order never
matters), (2) every payload field independently rotates the digest (no
input silently outside the key), and (3) the digests and the upstream
``ScopingStrategy.cache_key`` strings are STABLE — the golden tests turn
any change into a conscious, reviewable diff instead of silent cache
churn (or, worse, a writer/reader mismatch).
"""

from types import SimpleNamespace

import pytest

from fixtures.identity_stubs import install_identity_stubs
from fixtures.wrapper_spm import (
    installed_wrapper_has_storage_id,
    wrapper_storage_id as _wrapper_storage_id,
)
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

    def test_installed_capability_enters_the_dataset_identity(
        self, stub_identity_sources
    ):
        """The certified selection is part of what an artifact is.

        The stub above serves the pre-canonical answer so the goldens stay
        keyed to the synthetic version-set. This case takes the installed
        one: on a canonical bundle the resolved selection lands in the
        identity, which rotates every dataset and baseline key with it.
        """
        capability = stub_identity_sources.installed_spm_capability()
        if capability is None:
            pytest.skip("The installed bundle certifies no canonical SPM capability")
        stub_identity_sources.spm_capability = capability

        identity = ak.collect_dataset_identity("us", 2026)
        assert identity.spm == capability.defaults.model_dump()
        assert identity.digest != _DATASET_GOLDEN
        assert identity.digest == ak.dataset_key(
            **_DATASET_KWARGS, spm=capability.defaults.model_dump()
        )

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


_SPM_SELECTION = {
    "forecast_content_sha256": "a" * 64,
    "scenario": "ce_trend",
    "geography_kind": "national",
    "geography_id": None,
    "county_vintage": "2020",
    "as_of": None,
}
_INSTALLED_WRAPPER_HAS_STORAGE_ID = installed_wrapper_has_storage_id()
_SPM_STORAGE_GOLDEN = (
    "bl1-21f52b30719e20bb-spm-"
    "7396bf5f4876c42bb6cba0f9533478098edc0861cc3657bd0d60f88dbb26ac39"
)


class TestWrapperStorageIdAgreement:
    """The planner's storage id against the wrapper that names the file.

    Precompute plans a store path from ``BaselineArtifactIdentity.storage_id``
    and the in-container worker refuses to publish when the wrapper's own
    ``Simulation.storage_id`` disagrees. The wrapper's value also names the
    saved ``.h5``, so the two derivations are not merely compared — they are
    the same identifier reached down two independent code paths, and a
    disagreement blocks every canonical publish.

    This project now pins an SPM-capable ``policyengine``, so
    ``test_spm_arm_matches_the_installed_wrapper`` no longer skips: the SPM
    arm is checked against the real ``Simulation.storage_id``, not only
    against the transcription in ``fixtures/wrapper_spm.py``. It supplies
    the two things that wrapper's ``spm_config`` needs and this module
    otherwise does not — a US model version, and a bundle pinning the
    selection — because ``spm_config`` refuses a non-US model and
    re-resolves the selection through ``get_current_bundle`` rather than
    reading it off the object.

    The transcription stays for now because it is the second, independent
    derivation: precompute plans a store path from
    ``BaselineArtifactIdentity.storage_id`` and the in-container worker
    refuses to publish when the wrapper disagrees, and a test that asked the
    wrapper for both sides would prove nothing. What the cases prove:

    * the no-selection arm agrees through the exact accessor ``precompute``
      uses, against the real installed object — that arm must not move;
    * the SPM arm agrees with the wrapper's expression, so our side cannot
      drift from the contract without a reviewable diff, and the digest
      cannot be quietly reformatted.

    Agreement with the real wrapper was checked out of band on 2026-09-11 by
    running the executor environment with that wheel's ``policyengine``
    shadowing the pinned one, then resolving seven selection shapes (unset,
    empty, national, county, metro, a moved ``as_of``, and a non-ASCII
    scenario) through both the wheel's ``resolve_spm_selection`` and this
    repo's, and comparing the resolved configs and the storage ids. All
    seven agreed on both. That is a recorded observation, not coverage —
    only the native lane re-runs anything like it.

    The wheel's sha256 is load-bearing, not decoration: two builds both
    named ``policyengine-5.3.0-py3-none-any.whl`` are available locally, and
    the other one (``962882ea…``) has no ``storage_id`` at all.
    """

    @pytest.fixture
    def identity(self, stub_identity_sources):
        """Build the identity directly from an already-resolved selection.

        ``collect_baseline_identity`` resolves the selection through the
        installed bundle first; that resolution is the contract suite's
        subject. What is under test here is only what the identity then
        does with the resolved dict.
        """

        def _identity(spm=None):
            return ak.BaselineArtifactIdentity(
                spm=spm,
                country="us",
                region="national",
                scope_key=_BASELINE_KWARGS["scope_key"],
                dataset=ak.collect_dataset_identity("us", 2026),
            )

        return _identity

    @pytest.mark.parametrize(
        "selection",
        [
            None,
            _SPM_SELECTION,
            {**_SPM_SELECTION, "geography_kind": "county"},
            {**_SPM_SELECTION, "geography_kind": "metro", "geography_id": "35620"},
            {**_SPM_SELECTION, "as_of": "2025-01-01"},
            # Non-ASCII is the one input where our explicit ensure_ascii=True
            # could diverge from the wrapper's json.dumps defaults.
            {**_SPM_SELECTION, "scenario": "ce_trend_ü"},
        ],
    )
    def test_storage_id_matches_the_wrapper_expression(self, identity, selection):
        built = identity(selection)
        assert built.storage_id == _wrapper_storage_id(built.simulation_id, selection)

    def test_spm_storage_id_golden(self, identity):
        """Freeze the string. Changing it rotates every canonical artifact."""
        built = identity(_SPM_SELECTION)
        assert built.storage_id == _SPM_STORAGE_GOLDEN
        assert built.store_path.endswith(f"/{_SPM_STORAGE_GOLDEN}.h5")

    @pytest.mark.parametrize("field", sorted(_SPM_SELECTION))
    def test_every_selection_field_rotates_the_storage_id(self, identity, field):
        perturbed = {**_SPM_SELECTION, field: "other"}
        assert identity(perturbed).storage_id != _SPM_STORAGE_GOLDEN

    def test_storage_id_ignores_selection_key_order(self, identity):
        reversed_selection = dict(reversed(list(_SPM_SELECTION.items())))
        assert identity(reversed_selection).storage_id == _SPM_STORAGE_GOLDEN

    def test_no_selection_keeps_the_plain_simulation_id(self, identity):
        built = identity(None)
        assert built.storage_id == built.simulation_id
        assert built.store_path.endswith(f"/{built.simulation_id}.h5")

    def test_legacy_arm_matches_the_wrapper_accessor(self, identity):
        """The no-selection arm, through the accessor precompute uses.

        ``precompute`` reads ``getattr(baseline, "storage_id", baseline.id)``.
        With no resolved selection that has to be the planned id on *any*
        wrapper: pre-canonical, because the attribute is absent and the
        fallback is the id; canonical, because the property short-circuits
        to the id when ``spm_config`` is None. The assertion is the same
        either way, so this pins the accessor's contract rather than telling
        the two wrappers apart.

        "No resolved selection" is the condition, not "the caller sent no
        selection". On a canonical bundle the wrapper resolves an unset
        ``spm`` into the bundle's defaults and returns a suffixed id — and
        so does this project, through the same rule in
        ``resolve_spm_selection``, which is why they still agree. A plain id
        is what a bundle with no SPM measurement produces.
        """
        from policyengine.core import Simulation

        built = identity(None)
        wrapper = Simulation.model_construct(id=built.simulation_id)
        assert getattr(wrapper, "storage_id", wrapper.id) == built.storage_id

    @pytest.mark.skipif(
        not _INSTALLED_WRAPPER_HAS_STORAGE_ID,
        reason=(
            "The pinned policyengine is pre-canonical and has no storage_id; "
            "this asserts real equality as soon as an SPM-capable wrapper is "
            "pinned, replacing the transcribed expression above."
        ),
    )
    def test_spm_arm_matches_the_installed_wrapper(self, identity, monkeypatch):
        """Real equality against the wrapper, once one can be imported.

        The canonical ``spm_config`` reads the model version's country and
        re-resolves the selection through the installed bundle, so a bare
        ``model_construct(id=..., spm=...)`` raises "SPM selection is only
        supported by the US model" instead of comparing anything. Both are
        supplied here so this activates on a pin rather than erroring.
        """
        import policyengine.bundle
        from policyengine.core import Simulation

        built = identity(_SPM_SELECTION)
        # Only after the planner has read the real installed bundle: this
        # stub is for the wrapper's re-resolution, not for ours.
        monkeypatch.setattr(
            policyengine.bundle,
            "get_current_bundle",
            lambda: {"measurements": {"spm": _SPM_SELECTION}},
        )
        wrapper = Simulation.model_construct(
            id=built.simulation_id,
            spm=_SPM_SELECTION,
            tax_benefit_model_version=SimpleNamespace(country_code="us"),
        )
        assert wrapper.storage_id == built.storage_id
