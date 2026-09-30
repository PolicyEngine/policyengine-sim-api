"""Packaged UK local-authority metadata shared by all simulation workers."""

from __future__ import annotations

import csv
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
import pandas as pd
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
    UKLocalAuthorityMetadata,
)

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


def _resource_path(filename: str):
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
            raise ValueError(
                f"UK local-authority resource {filename!r} has invalid columns"
            )
        rows: list[dict[str, str]] = []
        for raw in reader:
            if any(raw.get(field) is None for field in expected_fields):
                raise ValueError(
                    f"UK local-authority resource {filename!r} has an incomplete row"
                )
            rows.append({field: str(raw[field]).strip() for field in expected_fields})
        return tuple(rows)


def _require_code(code: str, filename: str) -> None:
    if not _CODE_PATTERN.fullmatch(code):
        raise ValueError(
            f"UK local-authority resource {filename!r} contains invalid code {code!r}"
        )


def _load_names() -> Mapping[str, str]:
    filename = "names.csv"
    names: dict[str, str] = {}
    for row in _rows(filename, ("code", "name")):
        code = row["code"]
        name = row["name"]
        _require_code(code, filename)
        if not name or name == code:
            raise ValueError(
                f"UK local-authority resource {filename!r} has no name for {code!r}"
            )
        if code in names:
            raise ValueError(
                f"UK local-authority resource {filename!r} repeats {code!r}"
            )
        names[code] = name
    return MappingProxyType(names)


def _load_coordinates(filename: str) -> Mapping[str, LocalAuthorityCoordinate]:
    coordinates: dict[str, LocalAuthorityCoordinate] = {}
    for row in _rows(filename, ("code", "x", "y")):
        code = row["code"]
        _require_code(code, filename)
        if code in coordinates:
            raise ValueError(
                f"UK local-authority resource {filename!r} repeats {code!r}"
            )
        try:
            coordinate = LocalAuthorityCoordinate(x=int(row["x"]), y=int(row["y"]))
        except ValueError as error:
            raise ValueError(
                f"UK local-authority resource {filename!r} has invalid coordinates "
                f"for {code!r}"
            ) from error
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
        raise ValueError(
            "UK local-authority names must exactly cover the supported boundary versions"
        )
    if lad22.keys() - lad23.keys() != _LAD22_ONLY_CODES:
        raise ValueError("LAD22 distinguishing codes do not match its coordinate file")
    if lad23.keys() - lad22.keys() != _LAD23_ONLY_CODES:
        raise ValueError("LAD23 distinguishing codes do not match its coordinate file")
    for code in lad22.keys() & lad23.keys():
        if lad22[code] != lad23[code]:
            raise ValueError(
                "UK local-authority coordinate differs between boundary versions "
                f"for {code!r}"
            )
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
            raise ValueError(
                "UK dataset contains an invalid local-authority code"
            ) from error
    elif isinstance(value, str):
        code = value.strip()
    else:
        raise TypeError("UK dataset contains a non-text local-authority code")
    if not code:
        return None
    if not _CODE_PATTERN.fullmatch(code):
        raise ValueError(f"UK dataset contains invalid local-authority code {code!r}")
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
        raise ValueError("UK dataset contains no local-authority codes")
    supported_codes = load_uk_local_authority_resources().names.keys()
    unsupported_codes = observed_codes - supported_codes
    if unsupported_codes:
        first = min(unsupported_codes)
        raise ValueError(
            f"UK dataset contains unsupported local-authority code {first!r}"
        )

    matches = tuple(
        definition
        for definition in _BOUNDARY_DEFINITIONS
        if observed_codes & definition.distinguishing_codes
    )
    if len(matches) > 1:
        raise ValueError("UK dataset mixes LAD22 and LAD23 local-authority codes")
    if not matches:
        raise ValueError(
            "UK dataset local-authority configuration cannot be identified"
        )
    return UKLocalAuthorityMetadata(boundary_version=matches[0].boundary_version)


def detect_uk_local_authority_metadata(
    country: str,
    dataset: object,
) -> UKLocalAuthorityMetadata | None:
    """Inspect a complete dataset before any requested regional scoping."""

    if country != "uk":
        return None
    data = getattr(dataset, "data", None)
    entity_data = getattr(data, "entity_data", None)
    if not isinstance(entity_data, Mapping):
        raise TypeError("UK dataset contains no entity tables")
    household = entity_data.get("household")
    if household is None:
        raise ValueError("UK dataset contains no household table")
    household_frame = pd.DataFrame(household)
    if "la_code_oa" not in household_frame:
        raise ValueError("UK dataset household table contains no la_code_oa column")
    return detect_uk_local_authority_boundary_version(
        household_frame["la_code_oa"].tolist()
    )


def detect_uk_local_authority_metadata_from_hdf(
    dataset_path: str,
) -> UKLocalAuthorityMetadata:
    """Validate an installed UK HDF dataset against the packaged metadata."""

    observed_codes: set[object] = set()
    with pd.HDFStore(dataset_path, mode="r") as store:
        if "/household" not in store.keys():
            raise ValueError("UK dataset contains no household table")
        try:
            chunks = store.select(
                "household",
                columns=["la_code_oa"],
                chunksize=100_000,
            )
            for chunk in chunks:
                if "la_code_oa" not in chunk:
                    raise ValueError(
                        "UK dataset household table contains no la_code_oa column"
                    )
                observed_codes.update(chunk["la_code_oa"].drop_duplicates().tolist())
        except (KeyError, TypeError, ValueError) as error:
            if "la_code_oa" in str(error):
                raise ValueError(
                    "UK dataset household table contains no la_code_oa column"
                ) from error
            raise
    return detect_uk_local_authority_boundary_version(observed_codes)
