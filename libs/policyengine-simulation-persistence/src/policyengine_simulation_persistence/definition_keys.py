"""Definition keys: what identifies a simulation or a report, so it is found again.

Identical requests end on the same simulation and report rows because the
service finds each by a key of its definition (C1 · 24's answer 1a), stored
beside the row's random ID in ``definition_key``. A key is the SHA-256, in
lowercase hexadecimal, of an object's canonical JSON: keys sorted, no
spaces, UTF-8, UUIDs as lowercase strings, missing values null. The object
names ``key_version``, stored in its own column too, so a later change of
format never matches an older key, and ``record``, what it keys.

Only the service computes keys; the Public API compares the stored columns.
A definition names catalog rows by dataset name and region code, never by
ID, because each catalog version has its own rows: one report spans
releases, and each release's calculation is a new run of it. A simulation's
key holds everything that changes its numbers, the SPM selection included;
a report's holds its two sides and window size and no options (C1 · 24's
answer 4). A report also stores a ``dataset_independent_key``: its key
object without the dataset (revised answer 1a), which a national report
and its local-area report share. A household simulation names its stored
household in place of a dataset and region, and a household report its
household (answer 8).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Mapping

    from policyengine_simulation_persistence.economy_run_records import (
        SimulationChoice,
    )

# The format of every key this module builds.
KEY_VERSION: Final = 1
# A report's or simulation's type, as the key and the Public API name it.
_ECONOMY: Final = "economy"
_HOUSEHOLD: Final = "household"


def canonical_json(definition: Mapping[str, object]) -> str:
    """Return the definition's canonical JSON text.

    Raise ValueError for a number JSON can't write exactly (NaN or an
    infinity) and TypeError for a value it has no form for.
    """
    return json.dumps(
        definition,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_value,
    )


def _json_value(value: object) -> str:
    """Write a UUID as its lowercase string; refuse anything else JSON lacks."""
    if isinstance(value, UUID):
        return str(value)
    msg = f"a definition can't hold a {type(value).__name__}"
    raise TypeError(msg)


def definition_key(definition: Mapping[str, object]) -> str:
    """Return the SHA-256 of the definition's canonical JSON, in lowercase hex."""
    return hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()


def _side(choice: SimulationChoice) -> dict[str, object]:
    return {"policy_id": choice.policy_id, "dynamic_id": choice.dynamic_id}


def _selection(spm: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the SPM selection as given, or None when the request gave none."""
    return None if spm is None else dict(spm)


@dataclass(frozen=True)
class EconomySimulationDefinition:
    """What identifies an economy simulation: everything that changes its numbers.

    Its stored policy and dynamic (dynamics by ID, answer 1b), dataset name,
    region code and year (answer 1a), and the SPM selection as the request
    gave it, or None when it gave none (answer 4c). The service never
    filters a simulation, so its filter is null. Never a version.
    """

    country: str
    choice: SimulationChoice
    dataset_name: str
    region_code: str
    year: int
    spm: Mapping[str, Any] | None

    def key_object(self) -> dict[str, object]:
        """Return the object whose canonical JSON the simulation's key hashes."""
        return {
            "record": "simulation",
            "key_version": KEY_VERSION,
            "simulation_type": _ECONOMY,
            "country": self.country,
            **_side(self.choice),
            "dataset": self.dataset_name,
            "region": self.region_code,
            "year": self.year,
            "filter": None,
            "spm": _selection(self.spm),
        }

    @property
    def definition_key(self) -> str:
        """The simulation's ``definition_key``."""
        return definition_key(self.key_object())


@dataclass(frozen=True)
class EconomyReportDefinition:
    """What identifies an economy report or a budget window (answers 1a and 4).

    A report is its two simulations and its window's size (None for a single
    year), with no options of its own: each side's stored policy and dynamic,
    and the dataset name, region code, year and SPM selection both sides
    share, named once. Never a version: versions belong to runs.
    """

    country: str
    baseline: SimulationChoice
    reform: SimulationChoice
    dataset_name: str
    region_code: str
    year: int
    budget_window_size: int | None
    spm: Mapping[str, Any] | None

    def key_object(self) -> dict[str, object]:
        """Return the object whose canonical JSON the report's key hashes."""
        return {
            "record": "report",
            "key_version": KEY_VERSION,
            "report_type": _ECONOMY,
            "country": self.country,
            "baseline": _side(self.baseline),
            "reform": _side(self.reform),
            "dataset": self.dataset_name,
            "region": self.region_code,
            "year": self.year,
            "budget_window_size": self.budget_window_size,
            "spm": _selection(self.spm),
        }

    @property
    def definition_key(self) -> str:
        """The report's ``definition_key``."""
        return definition_key(self.key_object())

    def dataset_independent_key_object(self) -> dict[str, object]:
        """Return the key object without the dataset, under its own record name."""
        key_object = self.key_object()
        del key_object["dataset"]
        key_object["record"] = "report_dataset_independent"
        return key_object

    @property
    def dataset_independent_key(self) -> str:
        """The report's ``dataset_independent_key``."""
        return definition_key(self.dataset_independent_key_object())

    def simulation(
        self, side: Literal["baseline", "reform"], *, year: int | None = None
    ) -> EconomySimulationDefinition:
        """Return the simulation ``side`` names, in ``year`` (the report's own).

        A budget window's years each name their own year's simulations.
        """
        return EconomySimulationDefinition(
            country=self.country,
            choice=self.baseline if side == "baseline" else self.reform,
            dataset_name=self.dataset_name,
            region_code=self.region_code,
            year=self.year if year is None else year,
            spm=self.spm,
        )


@dataclass(frozen=True)
class HouseholdSimulationDefinition:
    """What identifies a household simulation (answers 1a and 8).

    Its stored policy and dynamic, the stored household's ID in place of a
    dataset and region, and the year. The household's saved SPM selection
    is part of the stored household, which the Public API's runtime never
    changes, so its ID names it. The service never filters a simulation, so
    its filter is null. Never a version.
    """

    country: str
    choice: SimulationChoice
    household_id: UUID
    year: int

    def key_object(self) -> dict[str, object]:
        """Return the object whose canonical JSON the simulation's key hashes."""
        return {
            "record": "simulation",
            "key_version": KEY_VERSION,
            "simulation_type": _HOUSEHOLD,
            "country": self.country,
            **_side(self.choice),
            "household_id": self.household_id,
            "year": self.year,
            "filter": None,
        }

    @property
    def definition_key(self) -> str:
        """The simulation's ``definition_key``."""
        return definition_key(self.key_object())


@dataclass(frozen=True)
class HouseholdReportDefinition:
    """What identifies a household report: its sides, household and year.

    Each side's stored policy and dynamic, the reform None for a report of
    the baseline alone, the stored household both sides calculate, and the
    year, with no options. Never a version.
    """

    country: str
    baseline: SimulationChoice
    reform: SimulationChoice | None
    household_id: UUID
    year: int

    @property
    def sides(self) -> tuple[Literal["baseline", "reform"], ...]:
        """Return each side the report calculates, the baseline's first."""
        return ("baseline",) if self.reform is None else ("baseline", "reform")

    def key_object(self) -> dict[str, object]:
        """Return the object whose canonical JSON the report's key hashes."""
        return {
            "record": "report",
            "key_version": KEY_VERSION,
            "report_type": _HOUSEHOLD,
            "country": self.country,
            "baseline": _side(self.baseline),
            "reform": None if self.reform is None else _side(self.reform),
            "household_id": self.household_id,
            "year": self.year,
            "budget_window_size": None,
        }

    @property
    def definition_key(self) -> str:
        """The report's ``definition_key``."""
        return definition_key(self.key_object())

    def dataset_independent_key_object(self) -> dict[str, object]:
        """Return the key object under its own record name.

        A household report has no dataset to leave out; the Public API
        requires the key with the other two.
        """
        return {**self.key_object(), "record": "report_dataset_independent"}

    @property
    def dataset_independent_key(self) -> str:
        """The report's ``dataset_independent_key``."""
        return definition_key(self.dataset_independent_key_object())

    def simulation(
        self, side: Literal["baseline", "reform"]
    ) -> HouseholdSimulationDefinition:
        """Return the simulation ``side`` names; raise ValueError for no reform."""
        choice = self.baseline if side == "baseline" else self.reform
        if choice is None:
            msg = "a household report of the baseline alone has no reform"
            raise ValueError(msg)
        return HouseholdSimulationDefinition(
            country=self.country,
            choice=choice,
            household_id=self.household_id,
            year=self.year,
        )
