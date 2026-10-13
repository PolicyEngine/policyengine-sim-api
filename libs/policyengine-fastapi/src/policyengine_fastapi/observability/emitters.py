from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .config import ObservabilityConfig

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .contracts import SimulationLifecycleEvent, TracerArtifactManifest


class NoOpSpan(AbstractContextManager["NoOpSpan"]):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        return None


class Observability(Protocol):
    config: ObservabilityConfig

    def emit_lifecycle_event(self, event: SimulationLifecycleEvent) -> None: ...

    def emit_counter(
        self,
        name: str,
        value: int = 1,
        attributes: Mapping[str, str] | None = None,
    ) -> None: ...

    def emit_histogram(
        self,
        name: str,
        value: float,
        attributes: Mapping[str, str] | None = None,
    ) -> None: ...

    def record_artifact_manifest(self, manifest: TracerArtifactManifest) -> None: ...

    def span(
        self, name: str, attributes: Mapping[str, Any] | None = None
    ) -> NoOpSpan: ...

    def flush(self) -> None: ...


@dataclass
class NoOpObservability:
    config: ObservabilityConfig = field(default_factory=ObservabilityConfig.disabled)

    def emit_lifecycle_event(self, event: SimulationLifecycleEvent) -> None:
        return None

    def emit_counter(
        self,
        name: str,
        value: int = 1,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        return None

    def emit_histogram(
        self,
        name: str,
        value: float,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        return None

    def record_artifact_manifest(self, manifest: TracerArtifactManifest) -> None:
        return None

    def span(self, name: str, attributes: Mapping[str, Any] | None = None) -> NoOpSpan:
        return NoOpSpan()

    def flush(self) -> None:
        return None
