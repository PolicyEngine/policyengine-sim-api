"""Small simulation doubles for executor tests that are not model tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def installed_spm_selection() -> dict[str, object] | None:
    """Return the installed US bundle's default SPM selection, if any."""
    from policyengine_simulation_executor.spm import (
        runtime_spm_capability,
    )

    capability = runtime_spm_capability()
    return None if capability is None else capability.defaults.model_dump()


def spm_receipt(
    selection: Mapping[str, object], year: int | str = "2026"
) -> dict[str, object]:
    """Build the smallest calculation record accepted by ``SPMProvenance``."""
    return {
        "forecast_id": "test-only",
        "forecast_sha256": selection["forecast_content_sha256"],
        "scenario": selection["scenario"],
        "geography_kind": selection["geography_kind"],
        "runtime_versions": {
            "policyengine": "test-only",
            "policyengine-core": "test-only",
            "policyengine-us": "test-only",
            "spm-calculator": "test-only",
        },
        "years": {str(year): {"status": "forecast"}},
        "geographies": [],
        "composition_method": "classified-inputs",
        "storage_method": "formula",
    }


def spm_capable_simulation(
    selection: Mapping[str, object] | None,
    year: int | str = "2026",
    **attributes: object,
) -> SimpleNamespace:
    """Return a stand-in for a simulation that measured ``selection``."""
    attributes.setdefault("on_first_ensure", lambda _callback: None)
    if selection is None:
        return SimpleNamespace(**attributes)
    return SimpleNamespace(
        spm_config=dict(selection),
        spm_provenance=lambda: spm_receipt(selection, year),
        **attributes,
    )
