"""Tests for packaged UK local-authority display metadata."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
)
from policyengine_simulation_executor.uk_local_authority_metadata import (
    detect_uk_area_dataset_metadata,
    detect_uk_local_authority_boundary_version,
    detect_uk_local_authority_metadata,
    detect_uk_local_authority_metadata_from_hdf,
    detect_uk_run_dataset_metadata,
    load_uk_local_authority_resources,
    routed_uk_area_dataset_name,
    uk_area_hdf_dataset_path,
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


def _routes_area_regions(monkeypatch, *, routed: bool) -> None:
    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "uk_area_regions_routed",
        lambda: routed,
    )


@pytest.mark.parametrize("region_code", [None, "uk", "country/england"])
def test_dataset_detector_requires_codes_unless_area_regions_are_routed(
    monkeypatch, region_code: str | None
) -> None:
    _routes_area_regions(monkeypatch, routed=False)

    with pytest.raises(ValueError, match="contains no la_code_oa column"):
        detect_uk_local_authority_metadata(
            "uk", _national_dataset(), region_code=region_code
        )


def test_dataset_detector_accepts_a_national_run_when_area_regions_are_routed(
    monkeypatch,
) -> None:
    _routes_area_regions(monkeypatch, routed=True)

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
    _routes_area_regions(monkeypatch, routed=True)

    with pytest.raises(ValueError, match="contains no la_code_oa column"):
        detect_uk_local_authority_metadata(
            "uk", _national_dataset(), region_code=region_code
        )


_NATIONAL_FILE = "microcosm_uk_2024_25.h5"
_LOCAL_AREA_FILE = "microcosm_uk_2024_25_dense.h5"


@pytest.mark.parametrize(
    ("release_kind", "region_datasets", "routed"),
    [
        ("local_area", {"national": _NATIONAL_FILE}, False),
        (
            "local_area",
            {"national": _NATIONAL_FILE, "constituency": _LOCAL_AREA_FILE},
            False,
        ),
        (
            "local_area",
            {
                "national": _NATIONAL_FILE,
                "constituency": _LOCAL_AREA_FILE,
                "local_authority": _LOCAL_AREA_FILE,
            },
            True,
        ),
        # policyengine.py registers no area regions for an area-coded national
        # release, although it routes both area types.
        (
            "area_coded_national",
            {
                "national": _NATIONAL_FILE,
                "constituency": _NATIONAL_FILE,
                "local_authority": _NATIONAL_FILE,
            },
            False,
        ),
        (
            None,
            {
                "national": _NATIONAL_FILE,
                "constituency": _LOCAL_AREA_FILE,
                "local_authority": _LOCAL_AREA_FILE,
            },
            False,
        ),
        # A breakdown builds both levels from one dataset.
        (
            "local_area",
            {
                "national": _NATIONAL_FILE,
                "constituency": _LOCAL_AREA_FILE,
                "local_authority": _NATIONAL_FILE,
            },
            False,
        ),
    ],
)
def test_area_regions_count_as_routed_only_to_one_local_area_dataset(
    monkeypatch,
    release_kind: str | None,
    region_datasets: dict[str, str],
    *,
    routed: bool,
) -> None:
    _bundle_routes(monkeypatch, region_datasets, release_kind=release_kind)

    assert uk_area_regions_routed() is routed


@pytest.mark.parametrize(
    ("country", "region", "routed", "omitted"),
    [
        ("uk", "uk", True, True),
        ("uk", "country/wales", True, True),
        ("uk", "uk", False, False),
        ("uk", "country/wales", False, False),
        ("uk", "constituency/E14001063", True, False),
        ("uk", "local_authority/E06000063", True, False),
        ("us", "us", True, False),
    ],
)
def test_area_breakdowns_are_omitted_only_from_routed_uk_wide_and_nation_runs(
    monkeypatch, country: str, region: str, *, routed: bool, omitted: bool
) -> None:
    from policyengine_simulation_executor import uk_local_authority_metadata

    _routes_area_regions(monkeypatch, routed=routed)

    assert (
        uk_local_authority_metadata.uk_area_breakdowns_omitted(country, region)
        is omitted
    )


def uk_manifest(
    region_datasets: dict[str, str], *, release_kind: str | None = "local_area"
) -> SimpleNamespace:
    """Return a UK manifest routing each region type to a bundle file."""
    return SimpleNamespace(
        regional_release_kind=release_kind,
        region_datasets={
            region_type: SimpleNamespace(path_template=path)
            for region_type, path in region_datasets.items()
        },
        datasets={
            "microcosm_uk_2024_25": SimpleNamespace(path=_NATIONAL_FILE),
            "microcosm_uk_2024_25_dense": SimpleNamespace(path=_LOCAL_AREA_FILE),
        },
    )


def _bundle_routes(
    monkeypatch,
    region_datasets: dict[str, str],
    *,
    release_kind: str | None = "local_area",
) -> None:
    """Install a UK manifest routing each region type to a bundle file."""
    manifest = uk_manifest(region_datasets, release_kind=release_kind)
    monkeypatch.setattr(
        "policyengine.provenance.manifest.get_release_manifest",
        lambda _country: manifest,
    )


def _routes_both_area_types_to(monkeypatch, path: str) -> None:
    _bundle_routes(
        monkeypatch,
        {
            "national": _NATIONAL_FILE,
            "constituency": path,
            "local_authority": path,
        },
    )


def test_routed_area_dataset_is_the_one_both_area_types_name(monkeypatch) -> None:
    _routes_both_area_types_to(monkeypatch, _LOCAL_AREA_FILE)

    assert routed_uk_area_dataset_name() == "microcosm_uk_2024_25_dense"


def test_routed_area_dataset_must_serve_both_area_types(monkeypatch) -> None:
    _bundle_routes(
        monkeypatch,
        {"constituency": _LOCAL_AREA_FILE, "local_authority": _NATIONAL_FILE},
    )

    with pytest.raises(ValueError, match="to different datasets"):
        routed_uk_area_dataset_name()


@pytest.mark.parametrize(
    "region_datasets",
    [
        {"constituency": _LOCAL_AREA_FILE},
        {"constituency": _LOCAL_AREA_FILE, "local_authority": "uncertified.h5"},
    ],
)
def test_routed_area_dataset_must_be_a_certified_bundle_dataset(
    monkeypatch, region_datasets: dict[str, str]
) -> None:
    _bundle_routes(monkeypatch, region_datasets)

    with pytest.raises(
        ValueError, match="routes local_authority regions to no certified dataset"
    ):
        routed_uk_area_dataset_name()


def _area_dataset(**household: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(entity_data={"household": pd.DataFrame(household)})
    )


# One household of a routed local-area dataset: the area codes, the region
# the disclosure check nests them in, and the survey household it came from.
_ROUTED_AREA_HOUSEHOLD = {
    "constituency_code_oa": ["E14001063"],
    "la_code_oa": ["E06000063"],
    "region_code_oa": ["SOUTH_EAST"],
    "source_household_id": ["1"],
}


def test_routed_dataset_detection_identifies_the_authority_boundaries() -> None:
    dataset = _area_dataset(
        constituency_code_oa=["E14001063", "W07000112"],
        la_code_oa=["E06000063", "W06000001"],
        region_code_oa=["SOUTH_EAST", "WALES"],
        source_household_id=["1", "2"],
    )

    metadata = detect_uk_area_dataset_metadata(dataset, dataset_name="local")

    assert metadata.boundary_version is UKLocalAuthorityBoundaryVersion.LAD23


@pytest.mark.parametrize("missing", list(_ROUTED_AREA_HOUSEHOLD))
def test_routed_area_dataset_needs_every_column_area_outputs_read(
    missing: str,
) -> None:
    columns = dict(_ROUTED_AREA_HOUSEHOLD)
    del columns[missing]

    with pytest.raises(
        ValueError,
        match=f"'local', which the bundle routes .* has no {missing} column",
    ):
        detect_uk_area_dataset_metadata(_area_dataset(**columns), dataset_name="local")


def _hdf_household(path, **columns: list[object]) -> str:
    pd.DataFrame(columns).to_hdf(
        path, key="household", format="table", data_columns=True
    )
    return str(path)


def test_area_hdf_dataset_is_the_installed_default_unless_routed_without_codes(
    monkeypatch, tmp_path
) -> None:
    national = _hdf_household(
        tmp_path / _NATIONAL_FILE, household_id=[1], region=["WALES"]
    )
    coded = _hdf_household(
        tmp_path / "coded.h5", household_id=[1], la_code_oa=["E06000001"]
    )
    monkeypatch.setattr(
        "policyengine.provenance.dataset_materialization.materialize_dataset",
        lambda *_args, **_kwargs: pytest.fail("no dataset should be downloaded"),
    )

    _bundle_routes(monkeypatch, {"national": _NATIONAL_FILE})
    assert uk_area_hdf_dataset_path(national) == national

    _routes_both_area_types_to(monkeypatch, _LOCAL_AREA_FILE)
    assert uk_area_hdf_dataset_path(coded) == coded


def test_area_hdf_dataset_downloads_the_routed_dataset_beside_the_default(
    monkeypatch, tmp_path
) -> None:
    national = _hdf_household(
        tmp_path / _NATIONAL_FILE, household_id=[1], region=["WALES"]
    )
    _routes_both_area_types_to(monkeypatch, _LOCAL_AREA_FILE)
    downloads: list[tuple[str, str, object]] = []

    def materialize(country, dataset, *, data_dir, **_kwargs):
        downloads.append((country, dataset, data_dir))
        return SimpleNamespace(path=str(data_dir / _LOCAL_AREA_FILE))

    monkeypatch.setattr(
        "policyengine.provenance.dataset_materialization.materialize_dataset",
        materialize,
    )

    assert uk_area_hdf_dataset_path(national) == str(tmp_path / _LOCAL_AREA_FILE)
    assert downloads == [("uk", "microcosm_uk_2024_25_dense", tmp_path)]


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


def test_breakdown_nations_are_the_uk_nations_policyengine_registers() -> None:
    from policyengine.countries.uk.regions import build_uk_region_registry
    from policyengine_simulation_contract import local_area_breakdown

    registry = build_uk_region_registry(
        include_constituencies=False, include_local_authorities=False
    )

    registered = frozenset(region.code for region in registry.get_by_type("country"))
    assert registered == local_area_breakdown.UK_NATION_REGIONS


def test_the_installed_bundle_routes_area_regions_to_the_dense_dataset() -> None:
    # Reads only the manifest packaged with the installed policyengine.py.
    from policyengine.countries.uk.regions import area_regions_routed_to_local_release
    from policyengine.provenance.manifest import get_release_manifest
    from policyengine_simulation_executor.simulation_runtime import (
        _manifest_dataset_revision,
    )

    name = routed_uk_area_dataset_name()
    revision = _manifest_dataset_revision("uk", name)

    assert name == "microcosm_uk_2024_25_dense"
    assert uk_area_regions_routed() is True
    assert uk_area_regions_routed() is area_regions_routed_to_local_release()
    assert isinstance(revision, str)
    assert revision.startswith("microcosm-uk-2024-25-dense-")
    assert revision == get_release_manifest("uk").datasets[name].revision


def _authority_codes_only() -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(
            entity_data={"household": pd.DataFrame({"la_code_oa": ["E06000063"]})}
        )
    )


def _routes_area_regions_to_the_dense_dataset(monkeypatch) -> None:
    _routes_area_regions(monkeypatch, routed=True)
    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "routed_uk_area_dataset_name",
        lambda: "microcosm_uk_2024_25_dense",
    )


def test_an_area_run_on_another_dataset_keeps_the_authority_code_check(
    monkeypatch,
) -> None:
    _routes_area_regions_to_the_dense_dataset(monkeypatch)

    metadata = detect_uk_run_dataset_metadata(
        "uk",
        _authority_codes_only(),
        region_code="local_authority/E06000063",
        dataset_name="enhanced_frs_2024_25",
    )

    assert metadata is not None
    assert metadata.boundary_version is UKLocalAuthorityBoundaryVersion.LAD23
    with pytest.raises(ValueError, match="contains no la_code_oa column"):
        detect_uk_run_dataset_metadata(
            "uk",
            _national_dataset(),
            region_code="constituency/E14001063",
            dataset_name="enhanced_frs_2024_25",
        )


def test_an_area_run_on_the_routed_dataset_needs_every_area_output_column(
    monkeypatch,
) -> None:
    _routes_area_regions_to_the_dense_dataset(monkeypatch)

    with pytest.raises(ValueError, match="has no constituency_code_oa column"):
        detect_uk_run_dataset_metadata(
            "uk",
            _authority_codes_only(),
            region_code="local_authority/E06000063",
            dataset_name="microcosm_uk_2024_25_dense",
        )
