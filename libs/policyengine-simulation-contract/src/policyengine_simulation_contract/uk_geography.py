"""Typed UK geography metadata shared by simulation execution paths."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class UKLocalAuthorityBoundaryVersion(StrEnum):
    """Supported UK local-authority boundary versions."""

    LAD22 = "lad22"
    LAD23 = "lad23"


class UKLocalAuthorityMetadata(BaseModel):
    """Boundary version detected from a complete UK simulation dataset."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    country: Literal["uk"] = "uk"
    boundary_version: UKLocalAuthorityBoundaryVersion
