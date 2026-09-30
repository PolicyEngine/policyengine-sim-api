"""Tests for packaged Stage 12 UK local-authority display metadata."""

from __future__ import annotations

from policyengine_simulation_contract.stage12_execution import (
    UKLocalAuthorityRoster,
)

from policyengine_simulation_executor.stage12_runtime.uk_local_authority_metadata import (
    load_uk_local_authority_resources,
)


LAD22_ONLY_CODES = {
    "E07000026",
    "E07000027",
    "E07000028",
    "E07000029",
    "E07000030",
    "E07000031",
    "E07000163",
    "E07000164",
    "E07000165",
    "E07000166",
    "E07000167",
    "E07000168",
    "E07000169",
    "E07000187",
    "E07000188",
    "E07000189",
    "E07000246",
}
LAD23_ONLY_CODES = {
    "E06000063",
    "E06000064",
    "E06000065",
    "E06000066",
}


def test_packaged_resources_cover_the_exact_supported_rosters() -> None:
    resources = load_uk_local_authority_resources()
    lad22 = resources.coordinates[UKLocalAuthorityRoster.LAD22]
    lad23 = resources.coordinates[UKLocalAuthorityRoster.LAD23]

    assert len(resources.names) == 378
    assert len(lad22) == 374
    assert len(lad23) == 361
    assert len(lad22.keys() & lad23.keys()) == 357
    assert lad22.keys() - lad23.keys() == LAD22_ONLY_CODES
    assert lad23.keys() - lad22.keys() == LAD23_ONLY_CODES
    assert resources.names.keys() == lad22.keys() | lad23.keys()


def test_shared_authorities_have_identical_coordinates() -> None:
    resources = load_uk_local_authority_resources()
    lad22 = resources.coordinates[UKLocalAuthorityRoster.LAD22]
    lad23 = resources.coordinates[UKLocalAuthorityRoster.LAD23]

    for code in lad22.keys() & lad23.keys():
        assert lad22[code] == lad23[code]


def test_resource_lookup_combines_names_with_roster_coordinates() -> None:
    resources = load_uk_local_authority_resources()

    cumberland = resources.metadata_for(UKLocalAuthorityRoster.LAD23)["E06000063"]
    assert cumberland.name == "Cumberland"
    assert isinstance(cumberland.x, int)
    assert isinstance(cumberland.y, int)
    assert "E06000063" not in resources.metadata_for(UKLocalAuthorityRoster.LAD22)
