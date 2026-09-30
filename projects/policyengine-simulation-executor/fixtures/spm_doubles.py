"""Small simulation doubles for executor tests that are not model tests."""

from types import SimpleNamespace


def installed_spm_selection():
    """Return the installed US bundle's default SPM selection, if any."""
    from policyengine_simulation_executor.spm import runtime_spm_capability

    capability = runtime_spm_capability()
    return None if capability is None else capability.defaults.model_dump()


def spm_receipt(selection, year="2026"):
    """Build the smallest calculation record accepted by ``SPMProvenance``."""
    return {
        "forecast_id": "test-only",
        "forecast_sha256": selection["forecast_content_sha256"],
        "scenario": selection["scenario"],
        "geography_kind": selection["geography_kind"],
        "runtime_versions": {"policyengine-us": "test-only"},
        "years": {str(year): {"status": "forecast"}},
        "geographies": [],
        "composition_method": "classified-inputs",
        "storage_method": "formula",
    }


def spm_capable_simulation(selection, year="2026", **attributes):
    """Return a stand-in for a simulation that measured ``selection``."""
    if selection is None:
        return SimpleNamespace(**attributes)
    return SimpleNamespace(
        spm_config=dict(selection),
        spm_provenance=lambda: spm_receipt(selection, year),
        **attributes,
    )
