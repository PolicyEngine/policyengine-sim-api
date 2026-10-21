"""Packaged UK local-authority metadata shared by all simulation workers."""

from __future__ import annotations

import csv
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

import pandas as pd
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
    UKLocalAuthorityMetadata,
)

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

_CODE_PATTERN = re.compile(r"^[A-Z]\d{8}$")
_RESOURCE_PACKAGE = "policyengine_simulation_executor"
_RESOURCE_DIRECTORY = ("static_runtime_files", "uk_local_authorities")
_LAD22_ONLY_CODES = frozenset(
    {
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
)
_LAD23_ONLY_CODES = frozenset(
    {
        "E06000063",
        "E06000064",
        "E06000065",
        "E06000066",
    }
)


@dataclass(frozen=True, slots=True)
class LocalAuthorityBoundaryDefinition:
    """Codes that distinguish one supported boundary configuration."""

    boundary_version: UKLocalAuthorityBoundaryVersion
    distinguishing_codes: frozenset[str]


_BOUNDARY_DEFINITIONS = (
    LocalAuthorityBoundaryDefinition(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD22,
        distinguishing_codes=_LAD22_ONLY_CODES,
    ),
    LocalAuthorityBoundaryDefinition(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD23,
        distinguishing_codes=_LAD23_ONLY_CODES,
    ),
)


@dataclass(frozen=True, slots=True)
class LocalAuthorityCoordinate:
    """Integer rendering coordinate for one local authority."""

    x: int
    y: int


@dataclass(frozen=True, slots=True)
class LocalAuthorityDisplayMetadata:
    """Display-only metadata attached after authority impacts are calculated."""

    name: str
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class UKLocalAuthorityResources:
    """Validated display names and coordinates for supported boundary versions."""

    names: Mapping[str, str]
    coordinates: Mapping[
        UKLocalAuthorityBoundaryVersion,
        Mapping[str, LocalAuthorityCoordinate],
    ]

    def metadata_for(
        self,
        boundary_version: UKLocalAuthorityBoundaryVersion,
    ) -> Mapping[str, LocalAuthorityDisplayMetadata]:
        """Combine common names with one boundary version's rendering coordinates."""
        return MappingProxyType(
            {
                code: LocalAuthorityDisplayMetadata(
                    name=self.names[code],
                    x=coordinate.x,
                    y=coordinate.y,
                )
                for code, coordinate in self.coordinates[boundary_version].items()
            }
        )


def _resource_path(filename: str) -> Traversable:
    path = files(_RESOURCE_PACKAGE)
    for part in _RESOURCE_DIRECTORY:
        path = path.joinpath(part)
    return path.joinpath(filename)


def _rows(
    filename: str, expected_fields: tuple[str, ...]
) -> tuple[dict[str, str], ...]:
    with _resource_path(filename).open("r", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != expected_fields:
            msg = f"UK local-authority resource {filename!r} has invalid columns"
            raise ValueError(msg)
        rows: list[dict[str, str]] = []
        for raw in reader:
            if any(raw.get(field) is None for field in expected_fields):
                msg = f"UK local-authority resource {filename!r} has an incomplete row"
                raise ValueError(msg)
            rows.append({field: str(raw[field]).strip() for field in expected_fields})
        return tuple(rows)


def _require_code(code: str, filename: str) -> None:
    if not _CODE_PATTERN.fullmatch(code):
        msg = f"UK local-authority resource {filename!r} contains invalid code {code!r}"
        raise ValueError(msg)


def _load_names() -> Mapping[str, str]:
    filename = "names.csv"
    names: dict[str, str] = {}
    for row in _rows(filename, ("code", "name")):
        code = row["code"]
        name = row["name"]
        _require_code(code, filename)
        if not name or name == code:
            msg = f"UK local-authority resource {filename!r} has no name for {code!r}"
            raise ValueError(msg)
        if code in names:
            msg = f"UK local-authority resource {filename!r} repeats {code!r}"
            raise ValueError(msg)
        names[code] = name
    return MappingProxyType(names)


def _load_coordinates(filename: str) -> Mapping[str, LocalAuthorityCoordinate]:
    coordinates: dict[str, LocalAuthorityCoordinate] = {}
    for row in _rows(filename, ("code", "x", "y")):
        code = row["code"]
        _require_code(code, filename)
        if code in coordinates:
            msg = f"UK local-authority resource {filename!r} repeats {code!r}"
            raise ValueError(msg)
        try:
            coordinate = LocalAuthorityCoordinate(x=int(row["x"]), y=int(row["y"]))
        except ValueError as error:
            msg = (
                f"UK local-authority resource {filename!r} has invalid coordinates "
                f"for {code!r}"
            )
            raise ValueError(msg) from error
        coordinates[code] = coordinate
    return MappingProxyType(coordinates)


@lru_cache(maxsize=1)
def load_uk_local_authority_resources() -> UKLocalAuthorityResources:
    """Load and validate every packaged local-authority resource."""
    names = _load_names()
    lad22 = _load_coordinates("coordinates_lad22.csv")
    lad23 = _load_coordinates("coordinates_lad23.csv")
    supported_codes = lad22.keys() | lad23.keys()
    if names.keys() != supported_codes:
        msg = (
            "UK local-authority names must exactly cover the supported "
            "boundary versions"
        )
        raise ValueError(msg)
    if lad22.keys() - lad23.keys() != _LAD22_ONLY_CODES:
        msg = "LAD22 distinguishing codes do not match its coordinate file"
        raise ValueError(msg)
    if lad23.keys() - lad22.keys() != _LAD23_ONLY_CODES:
        msg = "LAD23 distinguishing codes do not match its coordinate file"
        raise ValueError(msg)
    for code in lad22.keys() & lad23.keys():
        if lad22[code] != lad23[code]:
            msg = (
                "UK local-authority coordinate differs between boundary versions "
                f"for {code!r}"
            )
            raise ValueError(msg)
    return UKLocalAuthorityResources(
        names=names,
        coordinates=MappingProxyType(
            {
                UKLocalAuthorityBoundaryVersion.LAD22: lad22,
                UKLocalAuthorityBoundaryVersion.LAD23: lad23,
            }
        ),
    )


def _normalise_observed_code(value: object) -> str | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, bytes):
        try:
            code = value.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            msg = "UK dataset contains an invalid local-authority code"
            raise ValueError(msg) from error
    elif isinstance(value, str):
        code = value.strip()
    else:
        msg = "UK dataset contains a non-text local-authority code"
        raise TypeError(msg)
    if not code:
        return None
    if not _CODE_PATTERN.fullmatch(code):
        msg = f"UK dataset contains invalid local-authority code {code!r}"
        raise ValueError(msg)
    return code


def detect_uk_local_authority_boundary_version(
    values: Iterable[object],
) -> UKLocalAuthorityMetadata:
    """Identify LAD22 or LAD23 from the unscoped dataset's authority codes."""
    observed_codes = frozenset(
        code
        for value in values
        if (code := _normalise_observed_code(value)) is not None
    )
    if not observed_codes:
        msg = "UK dataset contains no local-authority codes"
        raise ValueError(msg)
    supported_codes = load_uk_local_authority_resources().names.keys()
    unsupported_codes = observed_codes - supported_codes
    if unsupported_codes:
        first = min(unsupported_codes)
        msg = f"UK dataset contains unsupported local-authority code {first!r}"
        raise ValueError(msg)

    matches = tuple(
        definition
        for definition in _BOUNDARY_DEFINITIONS
        if observed_codes & definition.distinguishing_codes
    )
    if len(matches) > 1:
        msg = "UK dataset mixes LAD22 and LAD23 local-authority codes"
        raise ValueError(msg)
    if not matches:
        msg = "UK dataset local-authority configuration cannot be identified"
        raise ValueError(msg)
    return UKLocalAuthorityMetadata(boundary_version=matches[0].boundary_version)


UK_AREA_REGION_TYPES = ("constituency", "local_authority")
UK_AREA_CODE_COLUMNS = ("constituency_code_oa", "la_code_oa")
_MISSING_AREA_CODES = (
    "UK dataset household table contains no la_code_oa column; a UK dataset "
    "may omit area codes only when the policyengine.py bundle routes "
    "constituency and local-authority regions to another dataset"
)


def uk_area_regions_routed() -> bool:
    """Whether the bundle routes UK constituency and local-authority regions.

    policyengine.py lists a region type in ``region_datasets`` when its
    ``regional_dataset_defaults`` names a dataset for it. Only when both area
    region types have one may the UK default, a national file, carry no area
    codes: their runs and breakdowns then belong to that local-area dataset.
    """
    from policyengine.provenance.manifest import (
        get_release_manifest,
    )

    region_datasets = get_release_manifest("uk").region_datasets
    return all(region_type in region_datasets for region_type in UK_AREA_REGION_TYPES)


def routed_uk_area_dataset_name() -> str:
    """Name the certified dataset the bundle routes UK area regions to.

    Like policyengine.py's ``region_dataset_name``, this is the bundle dataset
    whose path the region type's template names. Both area region types must
    share it: a national run builds both of its area breakdowns from one
    baseline and reform pair on that dataset.
    """
    from policyengine.provenance.manifest import (
        get_release_manifest,
    )

    manifest = get_release_manifest("uk")
    names: set[str] = set()
    for region_type in UK_AREA_REGION_TYPES:
        template = manifest.region_datasets.get(region_type)
        routed = [
            name
            for name, reference in manifest.datasets.items()
            if template is not None and reference.path == template.path_template
        ]
        if not routed:
            msg = f"The UK bundle routes {region_type} regions to no certified dataset"
            raise ValueError(msg)
        names.add(routed[0])
    if len(names) > 1:
        msg = (
            "The UK bundle routes constituency and local-authority regions to "
            f"different datasets ({', '.join(sorted(names))}); a national run "
            "builds both area breakdowns from one dataset"
        )
        raise ValueError(msg)
    return names.pop()


def uk_area_codes_required(region_code: str | None) -> bool:
    """Whether a UK run's dataset must carry area codes.

    A constituency or local-authority run always needs them. A national or
    nation-level run may use a dataset without them only when the bundle
    routes both area region types to another dataset.
    """
    region_type = (region_code or "").split("/", maxsplit=1)[0]
    if region_type in UK_AREA_REGION_TYPES:
        return True
    return not uk_area_regions_routed()


def detect_uk_local_authority_metadata(
    country: str,
    dataset: object,
    *,
    region_code: str | None = None,
) -> UKLocalAuthorityMetadata | None:
    """Inspect a complete dataset before any requested regional scoping.

    A UK dataset without ``la_code_oa`` passes, as ``None``, only for a run
    that does not need area codes (see ``uk_area_codes_required``); a missing
    column is never read as "this dataset is national-only" on its own.
    """
    if country != "uk":
        return None
    household_frame = _uk_household_frame(dataset)
    if "la_code_oa" not in household_frame:
        if uk_area_codes_required(region_code):
            raise ValueError(_MISSING_AREA_CODES)
        return None
    return detect_uk_local_authority_boundary_version(
        household_frame["la_code_oa"].tolist()
    )


def detect_uk_area_dataset_metadata(
    dataset: object, *, dataset_name: str
) -> UKLocalAuthorityMetadata:
    """Check a loaded dataset that UK area regions are routed to.

    That dataset serves constituency and local-authority results, so unlike a
    national default it must carry both area code columns, and its authority
    codes must match one packaged boundary version.
    """
    household_frame = _uk_household_frame(dataset)
    for column in UK_AREA_CODE_COLUMNS:
        if column not in household_frame:
            msg = (
                f"UK dataset {dataset_name!r}, which the bundle routes "
                f"constituency and local-authority regions to, has no {column} "
                "column"
            )
            raise ValueError(msg)
    return detect_uk_local_authority_boundary_version(
        household_frame["la_code_oa"].tolist()
    )


def _uk_household_frame(dataset: object) -> pd.DataFrame:
    data = getattr(dataset, "data", None)
    entity_data = getattr(data, "entity_data", None)
    if not isinstance(entity_data, Mapping):
        msg = "UK dataset contains no entity tables"
        raise TypeError(msg)
    household = entity_data.get("household")
    if household is None:
        msg = "UK dataset contains no household table"
        raise ValueError(msg)
    return pd.DataFrame(household)


def uk_area_hdf_dataset_path(installed_default_path: str) -> str:
    """Return the UK HDF file whose area codes the worker's results use.

    That is the installed default, unless it carries no area codes and the
    bundle routes constituency and local-authority regions to another
    dataset. Then the routed dataset is downloaded next to the default and
    checked against its bundle digest, and its path is returned. The download
    needs the Hugging Face token, as the data is private.
    """
    if not uk_area_regions_routed() or uk_hdf_household_has_local_authority_codes(
        installed_default_path
    ):
        return installed_default_path
    from policyengine.provenance.dataset_materialization import (
        materialize_dataset,
    )

    routed = materialize_dataset(
        "uk",
        routed_uk_area_dataset_name(),
        data_dir=Path(installed_default_path).parent,
    )
    return routed.path


def uk_hdf_household_has_local_authority_codes(dataset_path: str) -> bool:
    """Whether an installed UK HDF dataset's household table has ``la_code_oa``."""
    with pd.HDFStore(dataset_path, mode="r") as store:
        if "household" not in store:
            msg = "UK dataset contains no household table"
            raise ValueError(msg)
        household = store.select("household", stop=0)
    if not isinstance(household, pd.DataFrame):
        msg = "UK dataset household table is not a data frame"
        raise TypeError(msg)
    return "la_code_oa" in household.columns


def _require_local_authority_column(household_chunk: pd.DataFrame) -> None:
    if "la_code_oa" not in household_chunk:
        msg = "UK dataset household table contains no la_code_oa column"
        raise ValueError(msg)


def detect_uk_local_authority_metadata_from_hdf(
    dataset_path: str,
) -> UKLocalAuthorityMetadata:
    """Validate an installed UK HDF dataset against the packaged metadata."""
    observed_codes: set[object] = set()
    with pd.HDFStore(dataset_path, mode="r") as store:
        # keys() lists only pandas tables; ``in store`` would also match any
        # other HDF node named household.
        if "/household" not in store.keys(include="pandas"):
            msg = "UK dataset contains no household table"
            raise ValueError(msg)
        try:
            chunks = store.select(
                "household",
                columns=["la_code_oa"],
                chunksize=100_000,
            )
            for chunk in chunks:
                _require_local_authority_column(chunk)
                observed_codes.update(chunk["la_code_oa"].drop_duplicates().tolist())
        except (KeyError, TypeError, ValueError) as error:
            if "la_code_oa" in str(error):
                msg = "UK dataset household table contains no la_code_oa column"
                raise ValueError(msg) from error
            raise
    return detect_uk_local_authority_boundary_version(observed_codes)
