"""Tests for packaged UK local-authority display metadata."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
)

from policyengine_simulation_executor.uk_local_authority_metadata import (
    detect_uk_local_authority_metadata,
    detect_uk_local_authority_boundary_version,
    detect_uk_local_authority_metadata_from_hdf,
    load_uk_local_authority_resources,
    uk_area_regions_routed,
    uk_hdf_household_has_local_authority_codes,
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


def test_detector_ignores_missing_values_while_identifying_boundary_version() -> None:
    metadata = detect_uk_local_authority_boundary_version(
        [None, "", "  ", b"", float("nan"), pd.NA, "E07000026"]
    )

    assert metadata.boundary_version is UKLocalAuthorityBoundaryVersion.LAD22


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("E07000026", UKLocalAuthorityBoundaryVersion.LAD22),
        ("E06000063", UKLocalAuthorityBoundaryVersion.LAD23),
    ],
)
def test_dataset_detector_reads_the_complete_uk_household_table(
    code: str,
    expected: UKLocalAuthorityBoundaryVersion,
) -> None:
    dataset = SimpleNamespace(
        data=SimpleNamespace(
            entity_data={"household": pd.DataFrame({"la_code_oa": ["E06000001", code]})}
        )
    )

    metadata = detect_uk_local_authority_metadata("uk", dataset)

    assert metadata is not None
    assert metadata.boundary_version is expected


def test_dataset_detector_skips_non_uk_datasets() -> None:
    assert detect_uk_local_authority_metadata("us", object()) is None


def _national_dataset() -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(
            entity_data={"household": pd.DataFrame({"region": ["LONDON", "WALES"]})}
        )
    )


def _routes_area_regions(monkeypatch, routed: bool) -> None:
    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "uk_area_regions_routed",
        lambda: routed,
    )


@pytest.mark.parametrize("region_code", [None, "uk", "country/england"])
def test_dataset_detector_requires_codes_unless_area_regions_are_routed(
    monkeypatch, region_code: str | None
) -> None:
    _routes_area_regions(monkeypatch, False)

    with pytest.raises(ValueError, match="contains no la_code_oa column"):
        detect_uk_local_authority_metadata(
            "uk", _national_dataset(), region_code=region_code
        )


def test_dataset_detector_accepts_a_national_run_when_area_regions_are_routed(
    monkeypatch,
) -> None:
    _routes_area_regions(monkeypatch, True)

    assert (
        detect_uk_local_authority_metadata("uk", _national_dataset(), region_code="uk")
        is None
    )


@pytest.mark.parametrize(
    "region_code", ["constituency/E14001063", "local_authority/E06000063"]
)
def test_dataset_detector_requires_codes_for_area_runs_even_when_routed(
    monkeypatch, region_code: str
) -> None:
    _routes_area_regions(monkeypatch, True)

    with pytest.raises(ValueError, match="contains no la_code_oa column"):
        detect_uk_local_authority_metadata(
            "uk", _national_dataset(), region_code=region_code
        )


@pytest.mark.parametrize(
    ("region_types", "routed"),
    [
        (("national",), False),
        (("national", "constituency"), False),
        (("national", "constituency", "local_authority"), True),
    ],
)
def test_area_regions_count_as_routed_only_when_both_have_a_dataset(
    monkeypatch, region_types: tuple[str, ...], routed: bool
) -> None:
    monkeypatch.setattr(
        "policyengine.provenance.manifest.get_release_manifest",
        lambda country: SimpleNamespace(
            region_datasets={region_type: object() for region_type in region_types}
        ),
    )

    assert uk_area_regions_routed() is routed


def test_installed_hdf_detector_reads_local_authority_codes(tmp_path) -> None:
    dataset_path = tmp_path / "uk-dataset.h5"
    pd.DataFrame(
        {
            "household_id": [1, 2],
            "la_code_oa": ["E06000001", "E07000026"],
        }
    ).to_hdf(
        dataset_path,
        key="household",
        format="table",
        data_columns=True,
    )

    metadata = detect_uk_local_authority_metadata_from_hdf(str(dataset_path))

    assert uk_hdf_household_has_local_authority_codes(str(dataset_path))
    assert metadata.boundary_version is UKLocalAuthorityBoundaryVersion.LAD22


def test_installed_hdf_detector_rejects_a_dataset_without_codes(tmp_path) -> None:
    dataset_path = tmp_path / "uk-national-dataset.h5"
    pd.DataFrame({"household_id": [1, 2], "region": ["LONDON", "WALES"]}).to_hdf(
        dataset_path,
        key="household",
        format="table",
        data_columns=True,
    )

    assert not uk_hdf_household_has_local_authority_codes(str(dataset_path))
    with pytest.raises(ValueError, match="contains no la_code_oa column"):
        detect_uk_local_authority_metadata_from_hdf(str(dataset_path))


def test_detector_rejects_mixed_authority_configurations() -> None:
    with pytest.raises(ValueError, match="mixes LAD22 and LAD23"):
        detect_uk_local_authority_boundary_version(["E07000026", "E06000063"])


def test_detector_rejects_an_unidentifiable_configuration() -> None:
    with pytest.raises(ValueError, match="cannot be identified"):
        detect_uk_local_authority_boundary_version(["E06000001", "S12000033"])


@pytest.mark.parametrize(
    "value",
    [None, "", "  ", b"", float("nan"), pd.NA, "UNKNOWN", "E06000999"],
)
def test_detector_rejects_missing_or_unsupported_codes(value: object) -> None:
    with pytest.raises((TypeError, ValueError), match="local-authority code"):
        detect_uk_local_authority_boundary_version([value])
