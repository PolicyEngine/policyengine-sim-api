"""Observability emitter interface and the no-op implementation used today."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, Self

from .config import ObservabilityConfig

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

    from .contracts import SimulationLifecycleEvent, TracerArtifactManifest


class NoOpSpan(AbstractContextManager["NoOpSpan"]):
    """Span stand-in that accepts attributes and events and discards them."""

    def __enter__(self) -> Self:
        """Return the span itself."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Do nothing, so any exception raised inside the span propagates."""

    def set_attribute(self, key: str, value: object) -> None:
        """Discard the attribute."""

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        """Discard the event."""


class Observability(Protocol):
    """Emitter interface for lifecycle events, metrics, tracer manifests and spans."""

    config: ObservabilityConfig

    def emit_lifecycle_event(self, event: SimulationLifecycleEvent) -> None:
        """Record one lifecycle stage event for a run."""
        ...

    def emit_counter(
        self,
        name: str,
        value: int = 1,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        """Add ``value`` to the named counter."""
        ...

    def emit_histogram(
        self,
        name: str,
        value: float,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        """Record one observation in the named histogram."""
        ...

    def record_artifact_manifest(self, manifest: TracerArtifactManifest) -> None:
        """Record where a run's tracer capture was stored."""
        ...

    def span(self, name: str, attributes: Mapping[str, Any] | None = None) -> NoOpSpan:
        """Return a context manager covering the named span."""
        ...

    def flush(self) -> None:
        """Send any buffered telemetry."""
        ...


@dataclass
class NoOpObservability:
    """Observability that drops everything; returned even when emission is enabled."""

    config: ObservabilityConfig = field(default_factory=ObservabilityConfig.disabled)

    def emit_lifecycle_event(self, event: SimulationLifecycleEvent) -> None:
        """Discard the event."""

    def emit_counter(
        self,
        name: str,
        value: int = 1,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        """Discard the measurement."""

    def emit_histogram(
        self,
        name: str,
        value: float,
        attributes: Mapping[str, str] | None = None,
    ) -> None:
        """Discard the measurement."""

    def record_artifact_manifest(self, manifest: TracerArtifactManifest) -> None:
        """Discard the manifest."""

    def span(self, name: str, attributes: Mapping[str, Any] | None = None) -> NoOpSpan:
        """Return a span that records nothing."""
        return NoOpSpan()

    def flush(self) -> None:
        """Do nothing; there is never anything buffered."""
