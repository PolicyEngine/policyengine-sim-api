"""Compatibility schemas for the live synchronous simulation surface."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from policyengine_simulation_executor.simulation_macro_output import (
    SingleYearMacroOutput,
)


class SimulationOptions(BaseModel):
    """Legacy request schema name kept for generated clients."""

    country: str
    scope: Optional[str] = None
    data: Optional[str] = None
    time_period: Optional[str | int] = None
    reform: Optional[dict[str, Any]] = None
    baseline: Optional[dict[str, Any]] = None
    region: Optional[str] = None
    region_group: Optional[list[str]] = None
    title: Optional[str] = None
    include_cliffs: Optional[bool] = None
    model_version: Optional[str] = None
    data_version: Optional[str] = None
    # Accepted for parity with the gateway contract. The synchronous
    # surface always runs monolithically, so segmented=false is trivially
    # honored and segmented=true has no effect here.
    segmented: Optional[bool] = None

    model_config = ConfigDict(extra="forbid")


class EconomyComparison(SingleYearMacroOutput):
    """Legacy response schema name kept for generated clients."""
