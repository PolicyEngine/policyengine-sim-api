"""Tests for packaged Stage 12 UK local-authority display metadata."""

from __future__ import annotations

import pytest

from policyengine_simulation_contract.stage12_execution import (
    UKLocalAuthorityBoundaryVersion,
)

from policyengine_simulation_executor.stage12_runtime.uk_local_authority_metadata import (
    detect_uk_local_authority_boundary_version,
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


def test_packaged_resources_cover_the_exact_supported_boundary_versions() -> None:
    resources = load_uk_local_authority_resources()
    lad22 = resources.coordinates[UKLocalAuthorityBoundaryVersion.LAD22]
    lad23 = resources.coordinates[UKLocalAuthorityBoundaryVersion.LAD23]

    assert len(resources.names) == 378
    assert len(lad22) == 374
    assert len(lad23) == 361
    assert len(lad22.keys() & lad23.keys()) == 357
    assert lad22.keys() - lad23.keys() == LAD22_ONLY_CODES
    assert lad23.keys() - lad22.keys() == LAD23_ONLY_CODES
    assert resources.names.keys() == lad22.keys() | lad23.keys()


def test_shared_authorities_have_identical_coordinates() -> None:
    resources = load_uk_local_authority_resources()
    lad22 = resources.coordinates[UKLocalAuthorityBoundaryVersion.LAD22]
    lad23 = resources.coordinates[UKLocalAuthorityBoundaryVersion.LAD23]

    for code in lad22.keys() & lad23.keys():
        assert lad22[code] == lad23[code]


def test_resource_lookup_combines_names_with_boundary_coordinates() -> None:
    resources = load_uk_local_authority_resources()

    cumberland = resources.metadata_for(UKLocalAuthorityBoundaryVersion.LAD23)[
        "E06000063"
    ]
    assert cumberland.name == "Cumberland"
    assert isinstance(cumberland.x, int)
    assert isinstance(cumberland.y, int)
    assert "E06000063" not in resources.metadata_for(
        UKLocalAuthorityBoundaryVersion.LAD22
    )


def test_detector_identifies_lad22_from_a_predecessor_code() -> None:
    metadata = detect_uk_local_authority_boundary_version(
        ["E06000001", " E07000026 ", "S12000033"]
    )

    assert metadata.boundary_version is UKLocalAuthorityBoundaryVersion.LAD22


def test_detector_identifies_lad23_from_a_successor_code() -> None:
    metadata = detect_uk_local_authority_boundary_version(
        [b"E06000001", b"E06000063", b"S12000033"]
    )

    assert metadata.boundary_version is UKLocalAuthorityBoundaryVersion.LAD23


def test_detector_rejects_mixed_authority_configurations() -> None:
    with pytest.raises(ValueError, match="mixes LAD22 and LAD23"):
        detect_uk_local_authority_boundary_version(["E07000026", "E06000063"])


def test_detector_rejects_an_unidentifiable_configuration() -> None:
    with pytest.raises(ValueError, match="cannot be identified"):
        detect_uk_local_authority_boundary_version(["E06000001", "S12000033"])


@pytest.mark.parametrize("value", [None, "", "UNKNOWN", "E06000999"])
def test_detector_rejects_missing_or_unsupported_codes(value: object) -> None:
    with pytest.raises((TypeError, ValueError), match="local-authority code"):
        detect_uk_local_authority_boundary_version([value])
