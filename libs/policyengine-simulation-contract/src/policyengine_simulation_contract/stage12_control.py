"""Fail-closed runtime controls for Stage 12 evaluation execution."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

V2_DUAL_EXECUTION_CONTROL_NAME = "simulation-api-v2-dual-execution"
ACTIVE_CONTROL_KEY = "active"


class KeyValueStore(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def __setitem__(self, key: str, value: Any) -> None: ...


class Stage12DualExecutionControl(BaseModel):
    """One atomic environment-specific control document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    environment: str = Field(min_length=1, max_length=64)
    manifest_selection_enabled: bool = False
    economy_enabled: bool = False
    updated_at: datetime


class Stage12DualExecutionControlLoader:
    """Read every decision from shared state so disablement is immediate."""

    def __init__(self, store: KeyValueStore) -> None:
        self._store = store

    def load(self) -> Stage12DualExecutionControl | None:
        try:
            value = self._store.get(ACTIVE_CONTROL_KEY)
            return Stage12DualExecutionControl.model_validate(value)
        except Exception:
            return None

    def enabled(self, *, calculation_flow: str, environment: str) -> bool:
        control = self.load()
        if control is None or control.environment != environment:
            return False
        if not control.manifest_selection_enabled:
            return False
        if calculation_flow == "economy":
            return control.economy_enabled
        return False


def publish_control(
    store: KeyValueStore,
    control: Stage12DualExecutionControl,
) -> None:
    """Replace the complete control document with one store operation."""

    store[ACTIVE_CONTROL_KEY] = control.model_dump(mode="json")
