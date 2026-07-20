"""Unit tests for region-group resolution (B2) and the CD-impact gate (B3)."""

from unittest.mock import MagicMock

import pytest

from policyengine.core.scoping_strategy import (
    RegionGroupStrategy,
    RowFilterStrategy,
    WeightReplacementStrategy,
)
from policyengine_simulation_executor import simulation_runtime as sr
from policyengine_simulation_executor.simulation_output_geographic import (
    _should_build_us_congressional_district_impact as gate,
)


def _region_with(strategy):
    region = MagicMock()
    region.scoping_strategy = strategy
    return region


def _country_module(regions: dict):
    module = MagicMock()
    module.model.get_region.side_effect = lambda code: regions.get(code)
    return module


def _state(fips):
    return RowFilterStrategy(variable_name="state_fips", variable_value=fips)


class TestResolveRegionGroup:
    def test__builds_region_group_strategy_from_members(self, monkeypatch):
        monkeypatch.setattr(
            sr, "_resolve_dataset_reference", lambda c, p: "us-national"
        )
        cm = _country_module(
            {"state/hi": _region_with(_state(15)), "state/ia": _region_with(_state(19))}
        )
        res = sr._resolve_region_group(
            country_module=cm,
            country="us",
            params={"region_group": ["state/hi", "state/ia"]},
        )
        assert isinstance(res.scoping_strategy, RegionGroupStrategy)
        assert len(res.scoping_strategy.members) == 2
        assert res.code.startswith("region_group/")
        assert res.dataset_reference == "us-national"

    def test__code_is_member_order_independent(self, monkeypatch):
        monkeypatch.setattr(sr, "_resolve_dataset_reference", lambda c, p: "x")
        cm = _country_module(
            {"state/hi": _region_with(_state(15)), "state/ia": _region_with(_state(19))}
        )
        a = sr._resolve_region_group(
            country_module=cm,
            country="us",
            params={"region_group": ["state/hi", "state/ia"]},
        )
        b = sr._resolve_region_group(
            country_module=cm,
            country="us",
            params={"region_group": ["state/ia", "state/hi"]},
        )
        assert a.code == b.code

    def test__rejects_unknown_member(self, monkeypatch):
        monkeypatch.setattr(sr, "_resolve_dataset_reference", lambda c, p: "x")
        cm = _country_module({})  # get_region returns None
        with pytest.raises(ValueError, match="Unsupported"):
            sr._resolve_region_group(
                country_module=cm, country="us", params={"region_group": ["state/zz"]}
            )

    def test__rejects_weight_replacement_member(self, monkeypatch):
        monkeypatch.setattr(sr, "_resolve_dataset_reference", lambda c, p: "x")
        weight_replacement = WeightReplacementStrategy(
            weight_matrix_bucket="b",
            weight_matrix_key="k",
            lookup_csv_bucket="b",
            lookup_csv_key="l",
            region_code="E14001234",
        )
        cm = MagicMock()
        cm.model.get_region.return_value = _region_with(weight_replacement)
        with pytest.raises(ValueError, match="not a row-filter"):
            sr._resolve_region_group(
                country_module=cm, country="us", params={"region_group": ["state/hi"]}
            )

    def test__requires_nonempty_group(self, monkeypatch):
        monkeypatch.setattr(sr, "_resolve_dataset_reference", lambda c, p: "x")
        cm = _country_module({})
        with pytest.raises(ValueError, match="at least one"):
            sr._resolve_region_group(
                country_module=cm, country="us", params={"region_group": []}
            )

    def test__resolve_region_dispatches_to_group(self, monkeypatch):
        monkeypatch.setattr(sr, "_resolve_dataset_reference", lambda c, p: "x")
        cm = _country_module({"state/hi": _region_with(_state(15))})
        res = sr._resolve_region(
            country_module=cm, country="us", params={"region_group": ["state/hi"]}
        )
        assert isinstance(res.scoping_strategy, RegionGroupStrategy)


class TestCongressionalDistrictGate:
    def test__allows_national_state_and_group(self):
        assert gate(None) is True
        assert gate("us") is True
        assert gate("state/ca") is True
        assert gate("region_group/state/hi+state/ia") is True

    def test__rejects_district_and_place(self):
        assert gate("congressional_district/CA-01") is False
        assert gate("place/CA-12345") is False
