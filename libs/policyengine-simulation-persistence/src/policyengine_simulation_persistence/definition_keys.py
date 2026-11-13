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
releases, and each release's calculation is a new run of it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Mapping

    from policyengine_simulation_persistence.economy_run_records import (
        SimulationChoice,
    )

# The format of every key this module builds.
KEY_VERSION: Final = 1
# A report's type, as the key and the Public API name it.
_ECONOMY: Final = "economy"


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


@dataclass(frozen=True)
class EconomyReportDefinition:
    """What identifies an economy report or a budget window (answer 1a).

    Each side's stored policy and dynamic (dynamics by ID, answer 1b), the
    dataset name, region code and year both sides share, the window's size
    (None for a single year) and the options as ``reports.inputs`` stores
    them. Never a version: versions belong to runs.
    """

    country: str
    baseline: SimulationChoice
    reform: SimulationChoice
    dataset_name: str
    region_code: str
    year: int
    budget_window_size: int | None
    options: Mapping[str, Any]

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
            "options": dict(self.options),
        }

    @property
    def definition_key(self) -> str:
        """The report's ``definition_key``."""
        return definition_key(self.key_object())
