"""Tests for how a request's US or UK region resolves to its scoping and dataset."""

from types import SimpleNamespace

import pytest
from policyengine_simulation_executor.release_bundle import (
    get_country_release_bundle,
    resolve_runtime_bundle_dataset_uri,
)
from policyengine_simulation_executor.simulation_runtime import resolve_region


def test_resolve_region_scopes_us_state_from_national_dataset():
    bundle = get_country_release_bundle("us")
    scoping_strategy = object()
    state = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=scoping_strategy,
        parent_code="us",
    )
    national = SimpleNamespace(
        dataset_path=bundle.default_dataset_uri,
        scoping_strategy=None,
        parent_code=None,
    )
    regions = {"state/ut": state, "us": national}
    country_module = SimpleNamespace(model=SimpleNamespace(get_region=regions.get))

    resolution = resolve_region(
        country_module=country_module,
        country="us",
        params={"region": "state/UT"},
    )

    assert resolution.code == "state/ut"
    assert resolution.dataset_reference == resolve_runtime_bundle_dataset_uri(
        "us", None, prefer_local=False
    )
    assert resolution.scoping_strategy is scoping_strategy


def test_resolve_region_scopes_us_congressional_district_from_national_dataset():
    bundle = get_country_release_bundle("us")
    scoping_strategy = object()
    district = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=scoping_strategy,
        parent_code="state/ut",
    )
    state = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=object(),
        parent_code="us",
    )
    national = SimpleNamespace(
        dataset_path=bundle.default_dataset_uri,
        scoping_strategy=None,
        parent_code=None,
    )
    regions = {
        "congressional_district/UT-01": district,
        "state/ut": state,
        "us": national,
    }
    country_module = SimpleNamespace(model=SimpleNamespace(get_region=regions.get))

    resolution = resolve_region(
        country_module=country_module,
        country="us",
        params={"region": "congressional_district/ut-01"},
    )

    assert resolution.code == "congressional_district/UT-01"
    assert resolution.dataset_reference == resolve_runtime_bundle_dataset_uri(
        "us", None, prefer_local=False
    )
    assert resolution.scoping_strategy is scoping_strategy


def _us_country_module_with_regions(regions):
    return SimpleNamespace(model=SimpleNamespace(get_region=regions.get))


@pytest.mark.parametrize(
    ("requested_region", "state_code"),
    [
        ("state/ut", "UT"),
        ("state/UT", "UT"),
        ("ut", "UT"),
        ("congressional_district/ut-01", "UT"),
        ("congressional_district/WY-00", "WY"),
    ],
)
def test_resolve_region_reports_the_us_state_code(requested_region, state_code):
    utah = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=object(),
        parent_code="us",
        state_code="UT",
    )
    regions = {
        "us": SimpleNamespace(
            dataset_path=get_country_release_bundle("us").default_dataset_uri,
            scoping_strategy=None,
            parent_code=None,
            state_code=None,
        ),
        "state/ut": utah,
        "congressional_district/UT-01": SimpleNamespace(
            dataset_path=None,
            scoping_strategy=object(),
            parent_code="state/ut",
            state_code="UT",
        ),
        "congressional_district/WY-00": SimpleNamespace(
            dataset_path=None,
            scoping_strategy=object(),
            parent_code="state/wy",
            state_code="WY",
        ),
        "state/wy": SimpleNamespace(
            dataset_path=None,
            scoping_strategy=object(),
            parent_code="us",
            state_code="WY",
        ),
    }

    resolution = resolve_region(
        country_module=_us_country_module_with_regions(regions),
        country="us",
        params={"region": requested_region},
    )

    assert resolution.state_code == state_code


def test_resolve_region_reports_no_state_code_for_a_national_us_run():
    resolution = resolve_region(
        country_module=_us_country_module_with_regions({}),
        country="us",
        params={"region": "us"},
    )

    assert resolution.state_code is None


def test_resolve_region_reports_no_state_code_for_a_uk_region():
    scotland = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=object(),
        parent_code="uk",
        state_code=None,
    )
    national = SimpleNamespace(
        dataset_path=get_country_release_bundle("uk").default_dataset_uri,
        scoping_strategy=None,
        parent_code=None,
        state_code=None,
    )
    country_module = SimpleNamespace(
        model=SimpleNamespace(
            get_region={"country/scotland": scotland, "uk": national}.get
        )
    )

    resolution = resolve_region(
        country_module=country_module,
        country="uk",
        params={"region": "country/scotland"},
    )

    assert resolution.state_code is None


def test_resolve_region_reports_no_state_code_for_a_region_group():
    from policyengine.core.scoping_strategy import RowFilterStrategy

    regions = {
        code: SimpleNamespace(
            scoping_strategy=RowFilterStrategy(
                variable_name="state_fips", variable_value=fips
            ),
            state_code=code.removeprefix("state/").upper(),
        )
        for code, fips in (("state/ut", 49), ("state/wy", 56))
    }

    resolution = resolve_region(
        country_module=_us_country_module_with_regions(regions),
        country="us",
        params={"region": "us", "region_group": ["state/ut", "state/wy"]},
    )

    assert resolution.state_code is None


def test_every_installed_us_state_and_district_region_names_its_state():
    from policyengine.countries.us.regions import us_region_registry

    for region_type in ("state", "congressional_district"):
        regions = us_region_registry.get_by_type(region_type)
        assert regions
        for region in regions:
            assert region.state_code
            assert region.state_code == region.state_code.upper()


def test_resolve_region_rejects_unscoped_us_place_region():
    place = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=None,
        parent_code="state/ut",
    )
    state = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=object(),
        parent_code="us",
    )
    regions = {"place/UT-67000": place, "state/ut": state}
    country_module = SimpleNamespace(model=SimpleNamespace(get_region=regions.get))

    with pytest.raises(ValueError, match="US place regions are not yet supported"):
        resolve_region(
            country_module=country_module,
            country="us",
            params={"region": "place/ut-67000"},
        )


def test_resolve_region_scopes_uk_country_from_national_dataset():
    bundle = get_country_release_bundle("uk")
    scoping_strategy = object()
    england = SimpleNamespace(
        dataset_path=None,
        scoping_strategy=scoping_strategy,
        parent_code="uk",
    )
    uk = SimpleNamespace(
        dataset_path=bundle.default_dataset_uri,
        scoping_strategy=None,
        parent_code=None,
    )
    regions = {"country/england": england, "uk": uk}
    country_module = SimpleNamespace(model=SimpleNamespace(get_region=regions.get))

    resolution = resolve_region(
        country_module=country_module,
        country="uk",
        params={"region": "England"},
    )

    assert resolution.code == "country/england"
    assert resolution.dataset_reference == resolve_runtime_bundle_dataset_uri(
        "uk", None, prefer_local=False
    )
    assert resolution.scoping_strategy is scoping_strategy
