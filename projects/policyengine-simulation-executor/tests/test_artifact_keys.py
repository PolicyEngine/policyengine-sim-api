"""Artifact key discipline tests that hold whatever the identity schema.

The content-addressed store's correctness story rests on canonical digests
(field order never matters), on the upstream ``ScopingStrategy.cache_key``
strings the baseline key embeds being stable, and on the planner's storage
ID naming the same file .py's wrapper saves. The ``yf1`` and ``bl3`` payload
goldens live in ``test_prebuilt_identities``.
"""

from types import SimpleNamespace

import pytest
from fixtures.identity_stubs import install_identity_stubs
from fixtures.wrapper_spm import (
    wrapper_storage_id as _wrapper_storage_id,
)
from policyengine_simulation_executor import artifact_keys as ak

_GROUP_SCOPE_KEY = "region_group:row_filter:state_code=CA|row_filter:state_code=WV"


def test_canonical_digest_ignores_insertion_order():
    forward = {"a": 1, "b": {"c": 2, "d": None}}
    backward = {"b": {"d": None, "c": 2}, "a": 1}
    assert ak.canonical_digest(forward) == ak.canonical_digest(backward)


def test_simulation_id_shape(monkeypatch):
    from policyengine_simulation_executor.stage12_cache.levels import BaselineScope
    from policyengine_simulation_executor.stage12_cache.levels_us import (
        national_year_file,
    )

    install_identity_stubs(monkeypatch)
    identity = ak.baseline_identity(
        ak.year_file_identity(national_year_file(2026)),
        BaselineScope(region="national", scope_key=None),
    )
    simulation_id = identity.simulation_id
    assert simulation_id == f"bl3-{identity.digest[:16]}"
    # Filename-safe and disjoint from the year-file name pattern.
    assert "/" not in simulation_id
    assert "_year_" not in simulation_id


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
    """Stub the bundle/manifest/receipt seams the identity collectors read."""
    return install_identity_stubs(monkeypatch)


_SPM_SELECTION = {
    "forecast_content_sha256": "a" * 64,
    "scenario": "ce_trend",
    "geography_kind": "national",
    "geography_id": None,
    "county_vintage": "2020",
    "as_of": None,
}
_SPM_STORAGE_GOLDEN = (
    "bl3-3afa7cef3a4c4d91-spm-"
    "7396bf5f4876c42bb6cba0f9533478098edc0861cc3657bd0d60f88dbb26ac39"
)


class TestWrapperStorageIdAgreement:
    """The planner's storage id against PolicyEngine 6, which names the file.

    Precompute plans a store path from ``BaselineIdentity.storage_id``
    and the in-container worker refuses to publish when the wrapper's own
    ``Simulation.storage_id`` disagrees. The wrapper's value also names the
    saved ``.h5``, so the two derivations are not merely compared — they are
    the same identifier reached down two independent code paths, and a
    disagreement blocks every canonical publish.

    The no-selection arm exercises the accessor used by precompute. The SPM
    arm first freezes the digest expression for hermetic tests, then compares
    the same selections with the installed wrapper. The installed-wrapper
    cases provide the US model identity and a bundle containing the tested
    defaults because ``Simulation.spm_config`` resolves through that bundle.
    """

    @pytest.fixture
    def identity(self, stub_identity_sources):
        """Build the identity directly from an already-resolved selection.

        ``baseline_identity`` resolves the selection through the installed
        bundle first; that resolution is the contract suite's subject. What
        is under test here is only what the identity then does with the
        resolved dict.
        """
        from policyengine_simulation_executor.stage12_cache.levels_us import (
            national_year_file,
        )

        def _identity(spm=None):
            return ak.BaselineIdentity(
                spm=spm,
                region="national",
                scope_key=_GROUP_SCOPE_KEY,
                year_file=ak.year_file_identity(national_year_file(2026)),
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
        wrapper: older versions use the fallback because the attribute is
        absent; PolicyEngine 6's property returns the id when ``spm_config``
        is None. The assertion pins the accessor's behavior in either case.

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

    @pytest.mark.parametrize(
        "selection",
        [
            _SPM_SELECTION,
            {**_SPM_SELECTION, "geography_kind": "county"},
            {**_SPM_SELECTION, "geography_kind": "metro", "geography_id": "35620"},
            {**_SPM_SELECTION, "as_of": "2025-01-01"},
            {**_SPM_SELECTION, "scenario": "ce_trend_ü"},
        ],
    )
    def test_spm_arm_matches_the_installed_wrapper(
        self,
        identity,
        monkeypatch,
        selection,
    ):
        import policyengine.bundle
        from policyengine.core import Simulation

        built = identity(selection)
        monkeypatch.setattr(
            policyengine.bundle,
            "get_current_bundle",
            lambda: {"measurements": {"spm": selection}},
        )
        wrapper = Simulation.model_construct(
            id=built.simulation_id,
            spm=selection,
            tax_benefit_model_version=SimpleNamespace(country_code="us"),
        )
        assert wrapper.storage_id == built.storage_id
