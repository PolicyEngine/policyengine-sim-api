"""Packaged UK local-authority display metadata for temporary Stage 12 output."""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import Mapping

from policyengine_simulation_contract.stage12_execution import (
    UKLocalAuthorityMetadata,
    UKLocalAuthorityRoster,
)

_CODE_PATTERN = re.compile(r"^[A-Z]\d{8}$")
_RESOURCE_PACKAGE = "policyengine_simulation_executor"
_RESOURCE_DIRECTORY = ("resources", "uk_local_authorities")
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
class LocalAuthorityRosterDefinition:
    """Codes that distinguish one supported boundary configuration."""

    roster: UKLocalAuthorityRoster
    distinguishing_codes: frozenset[str]


_ROSTER_DEFINITIONS = (
    LocalAuthorityRosterDefinition(
        roster=UKLocalAuthorityRoster.LAD22,
        distinguishing_codes=_LAD22_ONLY_CODES,
    ),
    LocalAuthorityRosterDefinition(
        roster=UKLocalAuthorityRoster.LAD23,
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
    """Validated display names and coordinates for supported authority rosters."""

    names: Mapping[str, str]
    coordinates: Mapping[
        UKLocalAuthorityRoster,
        Mapping[str, LocalAuthorityCoordinate],
    ]

    def metadata_for(
        self,
        roster: UKLocalAuthorityRoster,
    ) -> Mapping[str, LocalAuthorityDisplayMetadata]:
        """Combine common names with one roster's rendering coordinates."""

        return MappingProxyType(
            {
                code: LocalAuthorityDisplayMetadata(
                    name=self.names[code],
                    x=coordinate.x,
                    y=coordinate.y,
                )
                for code, coordinate in self.coordinates[roster].items()
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
    """Load and validate every packaged Stage 12 local-authority resource."""

    names = _load_names()
    lad22 = _load_coordinates("coordinates_lad22.csv")
    lad23 = _load_coordinates("coordinates_lad23.csv")
    supported_codes = lad22.keys() | lad23.keys()
    if names.keys() != supported_codes:
        raise ValueError(
            "UK local-authority names must exactly cover the supported rosters"
        )
    if lad22.keys() - lad23.keys() != _LAD22_ONLY_CODES:
        raise ValueError("LAD22 distinguishing codes do not match its coordinate file")
    if lad23.keys() - lad22.keys() != _LAD23_ONLY_CODES:
        raise ValueError("LAD23 distinguishing codes do not match its coordinate file")
    for code in lad22.keys() & lad23.keys():
        if lad22[code] != lad23[code]:
            raise ValueError(
                f"UK local-authority coordinate differs between rosters for {code!r}"
            )
    return UKLocalAuthorityResources(
        names=names,
        coordinates=MappingProxyType(
            {
                UKLocalAuthorityRoster.LAD22: lad22,
                UKLocalAuthorityRoster.LAD23: lad23,
            }
        ),
    )


def _normalise_observed_code(value: object) -> str:
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
    if not _CODE_PATTERN.fullmatch(code):
        raise ValueError(f"UK dataset contains invalid local-authority code {code!r}")
    return code


def detect_uk_local_authority_roster(
    values: Iterable[object],
) -> UKLocalAuthorityMetadata:
    """Identify LAD22 or LAD23 from the unscoped dataset's authority codes."""

    observed_codes = frozenset(_normalise_observed_code(value) for value in values)
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
        for definition in _ROSTER_DEFINITIONS
        if observed_codes & definition.distinguishing_codes
    )
    if len(matches) > 1:
        raise ValueError("UK dataset mixes LAD22 and LAD23 local-authority codes")
    if not matches:
        raise ValueError(
            "UK dataset local-authority configuration cannot be identified"
        )
    return UKLocalAuthorityMetadata(roster=matches[0].roster)
