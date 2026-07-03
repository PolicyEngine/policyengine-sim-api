"""Legacy Logfire helpers used while evaluating a replacement platform."""

from __future__ import annotations

import os
from contextlib import nullcontext
from typing import Any

from policyengine_simulation_observability.observability import (
    logfire_replacement_attributes,
)


# Whether configure_logfire successfully configured export in this process.
# Logfire's own ``config.send_to_logfire`` flag defaults to ``True`` on an
# UNCONFIGURED instance, so it cannot be used to answer "did we configure
# Logfire?" — callers must consult this flag instead.
_logfire_configured = False


def legacy_logfire_attributes() -> dict[str, str]:
    return logfire_replacement_attributes()


def logfire_is_configured() -> bool:
    """Whether configure_logfire ran with a token in this process."""
    return _logfire_configured


def configure_logfire(
    service_name: str,
    *,
    default_environment: str | None = None,
) -> bool:
    """Configure legacy Logfire export if the Modal secret is present."""
    global _logfire_configured

    token = os.environ.get("LOGFIRE_TOKEN", "")
    if not token:
        _logfire_configured = False
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
    _logfire_configured = True
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
