"""Unit tests for the segmented-national partition (C2)."""

import re

from policyengine_simulation_executor.national_partition import (
    US_NATIONAL_REGION_GROUPS,
    national_region_groups,
)

# 50 states + DC as they appear in region codes.
ALL_STATE_CODES = {
    f"state/{s}"
    for s in (
        "al ak az ar ca co ct de dc fl ga hi id il in ia ks ky la me md ma "
        "mi mn ms mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx "
        "ut vt va wa wv wi wy"
    ).split()
}


class TestPartitionInvariants:
    def test__exactly_twenty_groups(self):
        assert len(US_NATIONAL_REGION_GROUPS) == 20

    def test__every_state_appears_exactly_once(self):
        codes = [c for group in US_NATIONAL_REGION_GROUPS for c in group]
        assert len(codes) == 51
        assert set(codes) == ALL_STATE_CODES

    def test__codes_are_wellformed_state_codes(self):
        for group in US_NATIONAL_REGION_GROUPS:
            # Guards the ("state/xx",) single-member tuple trap: a bare
            # ("state/xx") is a string and would iterate as characters.
            assert isinstance(group, tuple)
            for code in group:
                assert re.fullmatch(r"state/[a-z]{2}", code)

    def test__groups_are_nonempty(self):
        assert all(len(group) >= 1 for group in US_NATIONAL_REGION_GROUPS)


class TestPartitionMatchesModelRegistry:
    def test__partition_covers_exactly_the_registry_state_regions(self):
        """A policyengine pin bump that adds/renames a state-level region
        must fail here, forcing a measured partition rebalance. (At runtime
        the runner also assigns uncovered registry states to the last group
        as a stopgap — this test is what makes the drift loud in CI.)"""
        from policyengine_simulation_executor.simulation_runtime import (
            _country_module,
        )

        registry = _country_module("us").model.region_registry
        registry_states = {
            region.code
            for region in registry.regions
            if region.region_type == "state"
        }
        partition_codes = {
            code for group in US_NATIONAL_REGION_GROUPS for code in group
        }
        assert partition_codes == registry_states


class TestNationalRegionGroups:
    def test__us_returns_mutable_copies(self):
        groups = national_region_groups("us")
        assert groups is not None
        assert len(groups) == 20
        groups[0].append("state/zz")  # mutating the copy...
        assert "state/zz" not in US_NATIONAL_REGION_GROUPS[0]  # ...not the source

    def test__case_insensitive_country(self):
        assert national_region_groups("US") is not None

    def test__uk_has_no_partition(self):
        assert national_region_groups("uk") is None
