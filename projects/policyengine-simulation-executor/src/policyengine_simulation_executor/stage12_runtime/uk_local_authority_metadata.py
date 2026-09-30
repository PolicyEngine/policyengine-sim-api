"""Packaged UK local-authority display metadata for temporary Stage 12 output."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import Mapping

from policyengine_simulation_contract.stage12_execution import (
    UKLocalAuthorityRoster,
)

_CODE_PATTERN = re.compile(r"^[A-Z]\d{8}$")
_RESOURCE_PACKAGE = "policyengine_simulation_executor"
_RESOURCE_DIRECTORY = ("resources", "uk_local_authorities")


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
