"""Strict internal contracts for Stage 12 segment function calls."""

from __future__ import annotations

from hashlib import sha256
from typing import Annotated, Literal

from policyengine_simulation_contract.stage12_execution import (
    PlannedSimulationExecutionInput,
    Sha256Digest,
    SimulationRole,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictSegmentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Stage12SegmentInput(_StrictSegmentModel):
    """One single-policy region-group calculation."""

    schema_version: Literal[1] = 1
    simulation: PlannedSimulationExecutionInput
    segment_index: Annotated[int, Field(ge=0, lt=20)]
    region_codes: Annotated[tuple[str, ...], Field(min_length=1)]

    @field_validator("region_codes")
    @classmethod
    def require_unique_region_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("segment region codes must be unique")
        if any(not code.startswith("state/") for code in value):
            raise ValueError("segment region codes must identify state regions")
        return value

    @model_validator(mode="after")
    def require_us_simulation(self) -> Stage12SegmentInput:
        if self.simulation.geography.country != "us":
            raise ValueError("Stage 12 segments currently support only the US")
        if self.simulation.role not in {
            SimulationRole.BASELINE,
            SimulationRole.REFORM,
        }:
            raise ValueError("Stage 12 segment role must be baseline or reform")
        return self


class Stage12SegmentResult(_StrictSegmentModel):
    """Compressed, dtype-preserving output from one segment calculation."""

    schema_version: Literal[1] = 1
    segment_index: Annotated[int, Field(ge=0, lt=20)]
    role: SimulationRole
    region_codes: Annotated[tuple[str, ...], Field(min_length=1)]
    parquet_payload: Annotated[bytes, Field(min_length=1)]
    payload_sha256: Sha256Digest

    @field_validator("region_codes")
    @classmethod
    def require_unique_region_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("segment region codes must be unique")
        return value

    @field_validator("role")
    @classmethod
    def require_report_role(cls, value: SimulationRole) -> SimulationRole:
        if value not in {SimulationRole.BASELINE, SimulationRole.REFORM}:
            raise ValueError("Stage 12 segment role must be baseline or reform")
        return value

    @model_validator(mode="after")
    def require_payload_digest(self) -> Stage12SegmentResult:
        if sha256(self.parquet_payload).hexdigest() != self.payload_sha256:
            raise ValueError("segment payload digest differs from its bytes")
        return self
