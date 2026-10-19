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
        """Reject repeated region codes and codes that are not US states."""
        if len(value) != len(set(value)):
            msg = "segment region codes must be unique"
            raise ValueError(msg)
        if any(not code.startswith("state/") for code in value):
            msg = "segment region codes must identify state regions"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def require_us_simulation(self) -> Stage12SegmentInput:
        """Accept only US baseline and reform simulations."""
        if self.simulation.geography.country != "us":
            msg = "Stage 12 segments currently support only the US"
            raise ValueError(msg)
        if self.simulation.role not in {
            SimulationRole.BASELINE,
            SimulationRole.REFORM,
        }:
            msg = "Stage 12 segment role must be baseline or reform"
            raise ValueError(msg)
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
        """Reject repeated region codes."""
        if len(value) != len(set(value)):
            msg = "segment region codes must be unique"
            raise ValueError(msg)
        return value

    @field_validator("role")
    @classmethod
    def require_report_role(cls, value: SimulationRole) -> SimulationRole:
        """Accept only the baseline and reform roles."""
        if value not in {SimulationRole.BASELINE, SimulationRole.REFORM}:
            msg = "Stage 12 segment role must be baseline or reform"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def require_payload_digest(self) -> Stage12SegmentResult:
        """Require ``payload_sha256`` to be the SHA-256 of the payload bytes."""
        if sha256(self.parquet_payload).hexdigest() != self.payload_sha256:
            msg = "segment payload digest differs from its bytes"
            raise ValueError(msg)
        return self
