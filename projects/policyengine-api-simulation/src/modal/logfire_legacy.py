"""Legacy Logfire helpers used while evaluating a replacement platform."""

from __future__ import annotations

import os
from contextlib import nullcontext
from typing import Any

from policyengine_api_simulation.observability import (
    logfire_replacement_attributes,
)


def legacy_logfire_attributes() -> dict[str, str]:
    return logfire_replacement_attributes()


def configure_logfire(
    service_name: str,
    *,
    default_environment: str | None = None,
) -> bool:
    """Configure legacy Logfire export if the Modal secret is present."""
    token = os.environ.get("LOGFIRE_TOKEN", "")
    if not token:
        return False

    import logfire

    logfire.configure(
        service_name=service_name,
        token=token,
        environment=(
            os.environ.get("LOGFIRE_ENVIRONMENT")
            or default_environment
            or os.environ.get("MODAL_ENVIRONMENT")
            or "production"
        ),
        console=False,
    )
    return True


def logfire_span(enabled: bool, name: str, **attrs: Any):
    if not enabled:
        return nullcontext()

    import logfire

    return logfire.span(name, **attrs)


def flush_logfire(enabled: bool) -> None:
    if not enabled:
        return

    import logfire

    logfire.force_flush()
