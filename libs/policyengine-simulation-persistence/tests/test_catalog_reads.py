"""The Public API's catalog and policy rows a v2 economy run is resolved from."""

from __future__ import annotations

import pytest
from catalog_support import (
    CTC_AMOUNT,
    ENGLAND_REGIONS,
    INCOME_ELASTICITY,
    RETIRED_PARAMETER,
    US_STATES,
    CatalogRows,
    insert_catalog_rows,
)
from policyengine_simulation_persistence import (
    CatalogError,
    CatalogReader,
    CatalogVersion,
    EconomyResultLabels,
    GeographyLabel,
    PolicyParametersMissingError,
    RegionFilter,
    create_simulation_engine,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def catalog_rows(migrated_database) -> CatalogRows:
    return insert_catalog_rows(migrated_database.superuser_url)


@pytest.fixture
def reader(migrated_database) -> CatalogReader:
    """Read as the runtime role, which the operator grants SELECT on these."""
    return CatalogReader(create_simulation_engine(migrated_database.runtime_url))


def _us(rows: CatalogRows) -> CatalogVersion:
    return CatalogVersion(
        id=rows.us_version_id, country="us", policyengine_version="6.7.5"
    )


def test_a_catalog_version_is_found_by_country_and_policyengine_version(
    reader, catalog_rows
) -> None:
    assert reader.catalog_version("us", "6.7.5") == _us(catalog_rows)
    assert reader.catalog_version("uk", "6.7.5") == CatalogVersion(
        id=catalog_rows.uk_version_id, country="uk", policyengine_version="6.7.5"
    )
    assert reader.catalog_version("us", "6.8.0") is None
    assert reader.catalog_version_by_id(catalog_rows.older_us_version_id) == (
        CatalogVersion(
            id=catalog_rows.older_us_version_id,
            country="us",
            policyengine_version="6.5.0",
        )
    )


def test_a_state_uses_its_default_dataset_and_its_row_filter(
    reader, catalog_rows
) -> None:
    population = reader.population(_us(catalog_rows), "state/ca", None)

    assert population.region_code == "state/ca"
    assert population.dataset_id == catalog_rows.us_state_dataset_id
    assert population.dataset_name == "populace_us_2024_acs_local"
    assert population.row_filter == RegionFilter(field="state_fips", value="6")
    assert population.state_code == "CA"
    assert population.state_filters is None


def test_a_district_names_the_state_it_lies_in(reader, catalog_rows) -> None:
    population = reader.population(
        _us(catalog_rows), "congressional_district/CA-01", None
    )

    assert population.row_filter == RegionFilter(
        field="congressional_district_geoid", value="601"
    )
    assert population.state_code == "CA"


def test_a_simulation_can_name_another_dataset_of_the_version(
    reader, catalog_rows
) -> None:
    population = reader.population(_us(catalog_rows), "state/ca", "populace_us_2024")

    assert population.dataset_id == catalog_rows.us_national_dataset_id
    assert population.dataset_name == "populace_us_2024"


def test_the_us_has_no_filter_and_each_states_filter_for_its_segments(
    reader, catalog_rows
) -> None:
    population = reader.population(_us(catalog_rows), "us", None)

    assert population.row_filter is None
    assert population.dataset_name == "populace_us_2024"
    assert population.state_filters == {
        code: RegionFilter(field="state_fips", value=fips)
        for code, (fips, _state) in US_STATES.items()
    }


def test_a_uk_nation_keeps_the_list_of_regions_it_covers(reader, catalog_rows) -> None:
    uk = reader.catalog_version("uk", "6.7.5")
    assert uk is not None

    england = reader.population(uk, "country/england", None)
    national = reader.population(uk, "uk", None)

    assert england.row_filter == RegionFilter(field="region", value=ENGLAND_REGIONS)
    assert national.state_filters is None
    assert national.row_filter is None


@pytest.mark.parametrize(
    ("region_code", "dataset_name", "message"),
    [
        ("state/zz", None, "no region state/zz"),
        ("state/ca", "populace_us_2019", "no dataset populace_us_2019"),
        ("place/CA-57000", None, "weight_replacement"),
    ],
)
def test_a_region_or_dataset_the_version_cannot_run_is_refused(
    reader, catalog_rows, region_code, dataset_name, message
) -> None:
    with pytest.raises(CatalogError, match=message):
        reader.population(_us(catalog_rows), region_code, dataset_name)


def test_a_policy_saved_on_an_older_version_runs_by_parameter_name(
    reader, catalog_rows
) -> None:
    changes = reader.parameter_changes(
        catalog_rows.us_version_id, policy_id=catalog_rows.policy_id, dynamic_id=None
    )

    assert changes == {
        CTC_AMOUNT: {"2025-01-01.2025-12-31": 2500, "2026-01-01": 3000},
    }


def test_current_law_changes_no_parameter(reader, catalog_rows) -> None:
    assert (
        reader.parameter_changes(
            catalog_rows.us_version_id, policy_id=None, dynamic_id=None
        )
        == {}
    )


def test_a_dynamics_values_are_added_to_its_policys(reader, catalog_rows) -> None:
    changes = reader.parameter_changes(
        catalog_rows.us_version_id,
        policy_id=catalog_rows.policy_id,
        dynamic_id=catalog_rows.dynamic_id,
    )

    assert changes[INCOME_ELASTICITY] == {"2026-01-01": 0.1}
    assert changes[CTC_AMOUNT]["2026-01-01"] == 3000


def test_a_policy_changing_a_parameter_the_version_lacks_is_refused(
    reader, catalog_rows
) -> None:
    with pytest.raises(PolicyParametersMissingError) as raised:
        reader.parameter_changes(
            catalog_rows.us_version_id,
            policy_id=catalog_rows.retired_policy_id,
            dynamic_id=None,
        )

    assert raised.value.names == (RETIRED_PARAMETER,)
    assert RETIRED_PARAMETER in str(raised.value)


def test_uk_area_results_take_their_names_from_the_catalog(
    reader, catalog_rows
) -> None:
    labels = reader.area_labels(catalog_rows.uk_version_id)

    assert labels == EconomyResultLabels(
        constituencies={"E14001063": GeographyLabel(name="Aldershot", x=None, y=None)},
        local_authorities={
            "E06000063": GeographyLabel(name="Cumberland", x=None, y=None)
        },
    )
