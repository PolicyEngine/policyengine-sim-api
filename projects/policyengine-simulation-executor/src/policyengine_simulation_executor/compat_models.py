"""Compatibility schemas for the live synchronous simulation surface."""

from __future__ import annotations

from typing import Any

from policyengine_simulation_contract.macro_output import SingleYearMacroOutput
from policyengine_simulation_contract.spm import SPMSelection
from pydantic import BaseModel, ConfigDict


class SimulationOptions(BaseModel):
    """Legacy request schema name kept for generated clients."""

    country: str
    spm: SPMSelection | None = None
    scope: str | None = None
    time_period: str | int | None = None
    reform: dict[str, Any] | None = None
    baseline: dict[str, Any] | None = None
    region: str | None = None
    region_group: list[str] | None = None
    title: str | None = None
    include_cliffs: bool | None = None
    model_version: str | None = None
    # Accepted for parity with the gateway contract. The synchronous
    # surface always runs monolithically, so segmented=false is trivially
    # honored and segmented=true has no effect here.
    segmented: bool | None = None

    model_config = ConfigDict(extra="forbid")


class EconomyComparison(SingleYearMacroOutput):
    """Legacy response schema name kept for generated clients."""
