"""Diagnostic correlation identifiers shared by simulation services."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

OBSERVABILITY_ID_HEADER = "X-PolicyEngine-Observability-Id"


def generate_observability_id() -> str:
    """Create a diagnostic identifier with no application semantics."""

    return str(uuid4())


def resolve_observability_id(value: Any) -> str:
    """Use a valid caller value or create a new diagnostic identifier."""

    return normalize_observability_id(value) or generate_observability_id()


def normalize_observability_id(value: Any) -> str | None:
    """Return a canonical UUID string or ``None`` for malformed input.

    Observability metadata is best effort. Callers must never reject or alter a
    simulation because this value is absent or malformed.
    """

    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        return None
