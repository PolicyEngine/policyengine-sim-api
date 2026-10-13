"""Construction point for the observability emitter."""

from __future__ import annotations

from .config import ObservabilityConfig
from .emitters import NoOpObservability, Observability


def build_observability(
    config: ObservabilityConfig | None = None,
) -> Observability:
    """Central construction point for observability emitters.

    Commit 1 intentionally returns a no-op implementation even when enabled.
    Later commits can swap in a real backend here without changing callers.
    """
    if config is None:
        config = ObservabilityConfig.disabled()

    return NoOpObservability(config=config)


def get_observability(
    config: ObservabilityConfig | None = None,
) -> Observability:
    """Return an emitter for ``config``, or a disabled one when it is omitted."""
    return build_observability(config)
