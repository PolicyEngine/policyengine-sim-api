"""PolicyEngine Cloud Run Simulation Entrypoint.

``create_app`` is loaded on first use, so importing one of the package's
modules, such as the restart check that runs in the stuck-run sweep's Modal
image, never imports the web service and the packages only it needs.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from policyengine_simulation_entry.app import create_app

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:  # noqa: ANN401  # the attribute the app module defines
    """Load ``create_app`` from the app module when it is first asked for."""
    if name == "create_app":
        return import_module("policyengine_simulation_entry.app").create_app
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
